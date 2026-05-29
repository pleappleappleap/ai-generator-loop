package ai.image.pipeline.worker;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jdbc.core.support.AbstractSqlTypeValue;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.client.RestClient;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Types;
import java.util.List;
import java.util.Map;
import java.util.logging.Logger;
import java.util.stream.StreamSupport;

@Component
public class Scorer {

    private static final Logger log = Logger.getLogger(Scorer.class.getName());

    private static final List<String> VLM_SCORE_FIELDS = List.of(
            "photorealism", "anatomical_coherence",
            "interaction_plausibility", "lighting_consistency", "prompt_adherence");

    @Value("${scoring.clip-threshold:0.25}")
    private double clipThreshold;

    @Value("${scoring.artifact-threshold:0.50}")
    private double artifactThreshold;

    private final RestClient clipClient;
    private final RestClient artifactClient;
    private final RestClient vlmClient;
    private final NamedParameterJdbcTemplate jdbc;
    private final JmsTemplate jmsTemplate;
    private final ObjectMapper mapper;

    public Scorer(
            @Qualifier("clipScorerClient") RestClient clipClient,
            @Qualifier("artifactScorerClient") RestClient artifactClient,
            @Qualifier("vlmScorerClient") RestClient vlmClient,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper) {
        this.clipClient = clipClient;
        this.artifactClient = artifactClient;
        this.vlmClient = vlmClient;
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
    }

    @Transactional
    @JmsListener(destination = "loop.generated")
    public void onGenerated(String body) throws Exception {
        JsonNode msg = mapper.readTree(body);
        String imageUuid = msg.get("image_uuid").asText();
        String sessionUuid = msg.get("session_uuid").asText();
        int sequenceNumber = msg.get("sequence_number").asInt();
        String prompt = msg.path("prompt").asText("");
        String workflowPath = msg.path("workflow_path").asText("");
        String workflowParamsJson = mapper.writeValueAsString(
                msg.has("workflow_params") ? msg.get("workflow_params") : mapper.createObjectNode());
        String workflowId = nullIfBlank(msg.path("workflow_id").asText(""));
        String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));
        String imagePath = msg.path("image_path").asText("");

        log.info("Scoring image " + imageUuid);

        // ── CLIP sidecar ───────────────────────────────────────────────────────
        JsonNode clipResult = clipClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("image_uuid", imageUuid, "image_path", imagePath, "prompt", prompt))
                .retrieve()
                .body(JsonNode.class);
        double clipScore = clipResult.path("clip_score").asDouble(0.0);
        JsonNode imageEmbedding = clipResult.path("image_embedding");

        // ── Artifact sidecar ───────────────────────────────────────────────────
        JsonNode artifactResult = artifactClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("image_uuid", imageUuid, "image_path", imagePath))
                .retrieve()
                .body(JsonNode.class);
        double aiConfidence = artifactResult.path("ai_confidence").asDouble(1.0);

        // ── VLM sidecar ────────────────────────────────────────────────────────
        JsonNode vlmResult = vlmClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("image_uuid", imageUuid, "image_path", imagePath, "prompt", prompt))
                .retrieve()
                .body(JsonNode.class);

        // ── Threshold logic ────────────────────────────────────────────────────
        String verdict;
        String rejectionReason = null;
        if (clipScore < clipThreshold) {
            verdict = "rejected";
            rejectionReason = "clip_threshold";
        } else if (aiConfidence > artifactThreshold) {
            verdict = "rejected";
            rejectionReason = "artifact_threshold";
        } else {
            verdict = "candidate";
        }

        // ── VLM sub-fields ─────────────────────────────────────────────────────
        ArrayNode issuesNode = vlmResult.has("issues")
                ? (ArrayNode) vlmResult.get("issues") : mapper.createArrayNode();
        ArrayNode recsNode = vlmResult.has("recommendations")
                ? (ArrayNode) vlmResult.get("recommendations") : mapper.createArrayNode();
        final String[] issues = StreamSupport.stream(issuesNode.spliterator(), false)
                .map(JsonNode::asText).toArray(String[]::new);
        final String[] recs = StreamSupport.stream(recsNode.spliterator(), false)
                .map(JsonNode::asText).toArray(String[]::new);
        ObjectNode vlmScores = mapper.createObjectNode();
        for (String field : VLM_SCORE_FIELDS) {
            if (vlmResult.has(field)) vlmScores.set(field, vlmResult.get(field));
        }

        // ── Upsert into images ─────────────────────────────────────────────────
        String sql = """
                INSERT INTO images (
                    image_uuid, session_uuid, sequence_number, prompt,
                    workflow_path, workflow_params, workflow_id, conversation_id,
                    clip_score, artifact_score, vlm_scores, vlm_issues, vlm_recs,
                    verdict, rejection_reason, image_path, embedding
                ) VALUES (
                    :imageUuid::uuid, :sessionUuid::uuid, :seqNum, :prompt,
                    :workflowPath, :workflowParams::jsonb, :workflowId::uuid, :conversationId::uuid,
                    :clipScore, :artifactScore, :vlmScores::jsonb, :vlmIssues, :vlmRecs,
                    :verdict, :rejectionReason, :imagePath, :embedding::vector
                )
                ON CONFLICT (image_uuid) DO UPDATE SET
                    clip_score       = EXCLUDED.clip_score,
                    artifact_score   = EXCLUDED.artifact_score,
                    vlm_scores       = EXCLUDED.vlm_scores,
                    vlm_issues       = EXCLUDED.vlm_issues,
                    vlm_recs         = EXCLUDED.vlm_recs,
                    verdict          = EXCLUDED.verdict,
                    rejection_reason = EXCLUDED.rejection_reason,
                    embedding        = EXCLUDED.embedding
                """;

        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("imageUuid", imageUuid)
                .addValue("sessionUuid", sessionUuid)
                .addValue("seqNum", sequenceNumber)
                .addValue("prompt", prompt)
                .addValue("workflowPath", workflowPath)
                .addValue("workflowParams", workflowParamsJson)
                .addValue("workflowId", workflowId)
                .addValue("conversationId", conversationId)
                .addValue("clipScore", clipScore)
                .addValue("artifactScore", aiConfidence)
                .addValue("vlmScores", mapper.writeValueAsString(vlmScores))
                .addValue("vlmIssues", new AbstractSqlTypeValue() {
                    @Override
                    protected Object createTypeValue(Connection conn, int sqlType, String typeName)
                            throws SQLException {
                        return conn.createArrayOf("text", issues);
                    }
                }, Types.ARRAY)
                .addValue("vlmRecs", new AbstractSqlTypeValue() {
                    @Override
                    protected Object createTypeValue(Connection conn, int sqlType, String typeName)
                            throws SQLException {
                        return conn.createArrayOf("text", recs);
                    }
                }, Types.ARRAY)
                .addValue("verdict", verdict)
                .addValue("rejectionReason", rejectionReason)
                .addValue("imagePath", imagePath)
                .addValue("embedding", imageEmbedding.toString());

        jdbc.update(sql, params);
        log.info("Stored image " + imageUuid + " verdict=" + verdict);

        // ── Publish to loop.verdicts for candidates only ───────────────────────
        if ("candidate".equals(verdict)) {
            ObjectNode verdictMsg = buildVerdictMessage(
                    imageUuid, sessionUuid, sequenceNumber, prompt,
                    workflowPath, workflowId, conversationId,
                    clipScore, aiConfidence, vlmScores, issues, recs);
            jmsTemplate.convertAndSend("loop.verdicts", mapper.writeValueAsString(verdictMsg));
            log.info("Published verdict for " + imageUuid);
        }
    }

    private ObjectNode buildVerdictMessage(
            String imageUuid, String sessionUuid, int sequenceNumber,
            String prompt, String workflowPath,
            String workflowId, String conversationId,
            double clipScore, double artifactScore, ObjectNode vlmScores,
            String[] issues, String[] recs) {
        ObjectNode scores = mapper.createObjectNode()
                .put("clip_score", clipScore)
                .put("artifact_score", artifactScore);
        scores.set("vlm_scores", vlmScores);

        ObjectNode msg = mapper.createObjectNode();
        msg.put("image_uuid", imageUuid);
        msg.put("verdict", "candidate");
        msg.put("session_uuid", sessionUuid);
        msg.put("sequence_number", sequenceNumber);
        msg.put("prompt", prompt);
        msg.put("workflow_path", workflowPath);
        msg.put("workflow_id", workflowId != null ? workflowId : "");
        msg.put("conversation_id", conversationId != null ? conversationId : "");
        msg.set("scores", scores);
        ArrayNode issuesNode = mapper.createArrayNode();
        for (String i : issues) issuesNode.add(i);
        msg.set("vlm_issues", issuesNode);
        ArrayNode recsNode = mapper.createArrayNode();
        for (String r : recs) recsNode.add(r);
        msg.set("vlm_recs", recsNode);
        return msg;
    }

    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }
}
