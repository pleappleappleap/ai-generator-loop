package org.soxhlet.pipeline.worker;

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
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
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
    private final TransactionTemplate requiresNew;

    public Scorer(
            @Qualifier("clipScorerClient") RestClient clipClient,
            @Qualifier("artifactScorerClient") RestClient artifactClient,
            @Qualifier("vlmScorerClient") RestClient vlmClient,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            PlatformTransactionManager txManager) {
        this.clipClient = clipClient;
        this.artifactClient = artifactClient;
        this.vlmClient = vlmClient;
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
        this.requiresNew = new TransactionTemplate(txManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    // -- JMS listener - stage only, return immediately --------------------------

    @Transactional
    @JmsListener(destination = "loop.generated")
    public void onGenerated(String body) throws Exception {
        JsonNode msg = mapper.readTree(body);
        jdbc.update(
                "INSERT INTO pending_scorings (image_uuid, session_uuid, payload) " +
                "VALUES (:id::uuid, :sess::uuid, :payload::jsonb) ON CONFLICT DO NOTHING",
                Map.of(
                        "id", msg.get("image_uuid").asText(),
                        "sess", msg.get("session_uuid").asText(),
                        "payload", body));
    }

    // -- Polling loop - processes staged work (normal path + crash recovery) ----

    @Scheduled(fixedDelay = 2000)
    public void pollAndProcess() {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT image_uuid::text, payload::text FROM pending_scorings LIMIT 1",
                Map.of());
        if (rows.isEmpty()) return;
        Map<String, Object> row = rows.get(0);
        String imageUuid  = (String) row.get("image_uuid");
        String payloadJson = (String) row.get("payload");
        try {
            JsonNode msg = mapper.readTree(payloadJson);
            scoreImage(imageUuid, msg);
        } catch (Exception e) {
            log.warning("Scoring failed for " + imageUuid + ": " + e.getMessage());
        }
    }

    // -- Core scoring logic -----------------------------------------------------

    private void scoreImage(String imageUuid, JsonNode msg) throws Exception {
        String sessionUuid      = msg.get("session_uuid").asText();
        int    sequenceNumber   = msg.get("sequence_number").asInt();
        String prompt           = msg.path("prompt").asText("");
        String workflowPath     = msg.path("workflow_path").asText("");
        String workflowParamsJson = mapper.writeValueAsString(
                msg.has("workflow_params") ? msg.get("workflow_params") : mapper.createObjectNode());
        String workflowId       = nullIfBlank(msg.path("workflow_id").asText(""));
        String conversationId   = nullIfBlank(msg.path("conversation_id").asText(""));
        String imagePath        = msg.path("image_path").asText("");

        log.info("Scoring image " + imageUuid);

        // -- CLIP sidecar -------------------------------------------------------
        JsonNode clipResult = clipClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("image_uuid", imageUuid, "image_path", imagePath, "prompt", prompt))
                .retrieve()
                .body(JsonNode.class);
        double clipScore = clipResult.path("clip_score").asDouble(0.0);
        JsonNode imageEmbedding = clipResult.path("image_embedding");
        // Store in pgvector text format ([f1,f2,...]) so the column needs no fixed dimension.
        // Cast to vector at similarity query time with ::vector.
        String embeddingStr = null;
        if (imageEmbedding != null && imageEmbedding.isArray() && imageEmbedding.size() > 0) {
            StringBuilder sb = new StringBuilder("[");
            for (int k = 0; k < imageEmbedding.size(); k++) {
                if (k > 0) sb.append(',');
                sb.append(imageEmbedding.get(k).asDouble());
            }
            sb.append(']');
            embeddingStr = sb.toString();
        }

        // -- Artifact sidecar ---------------------------------------------------
        JsonNode artifactResult = artifactClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("image_uuid", imageUuid, "image_path", imagePath))
                .retrieve()
                .body(JsonNode.class);
        double aiConfidence = artifactResult.path("ai_confidence").asDouble(1.0);

        // -- VLM sidecar --------------------------------------------------------
        String vlmEvalPrompt = null;
        if (conversationId != null) {
            List<String> prompts = jdbc.queryForList(
                    "SELECT eval_prompt FROM session_vlm_prompts " +
                    "WHERE conversation_id = :convId::uuid ORDER BY created_at DESC LIMIT 1",
                    Map.of("convId", conversationId), String.class);
            if (!prompts.isEmpty()) vlmEvalPrompt = prompts.get(0);
        }
        java.util.Map<String, Object> vlmReqBody = new java.util.HashMap<>();
        vlmReqBody.put("image_uuid", imageUuid);
        vlmReqBody.put("image_path", imagePath);
        vlmReqBody.put("prompt", prompt);
        if (vlmEvalPrompt != null) vlmReqBody.put("eval_prompt", vlmEvalPrompt);
        JsonNode vlmResult = vlmClient.post().uri("/score")
                .contentType(MediaType.APPLICATION_JSON)
                .body(vlmReqBody)
                .retrieve()
                .body(JsonNode.class);

        // -- North star similarity ----------------------------------------------
        Double northStarSimilarity = null;
        if (embeddingStr != null) {
            List<Double> simRows = jdbc.queryForList(
                    "SELECT (1 - (ns.embedding::vector <=> :emb::vector))::float8 " +
                    "FROM north_star ns " +
                    "WHERE ns.superseded_at IS NULL AND ns.embedding IS NOT NULL " +
                    "ORDER BY ns.id DESC LIMIT 1",
                    Map.of("emb", embeddingStr), Double.class);
            if (!simRows.isEmpty()) northStarSimilarity = simRows.get(0);
        }

        // -- Threshold logic ----------------------------------------------------
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

        // -- VLM sub-fields -----------------------------------------------------
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

        // -- Tx2: delete staging row, upsert scores, publish verdict -----------
        final String  finalVerdict         = verdict;
        final String  finalRejectionReason = rejectionReason;
        final String  finalEmbedding        = embeddingStr;
        final Double  finalNorthStarSim    = northStarSimilarity;

        requiresNew.execute(status -> {
            try {
                jdbc.update("DELETE FROM pending_scorings WHERE image_uuid = :id::uuid",
                        Map.of("id", imageUuid));

                String sql = """
                        INSERT INTO images (
                            image_uuid, session_uuid, sequence_number, prompt,
                            workflow_path, workflow_params, workflow_id, conversation_id,
                            clip_score, artifact_score, vlm_scores, vlm_issues, vlm_recs,
                            verdict, rejection_reason, image_path, embedding, north_star_similarity
                        ) VALUES (
                            :imageUuid::uuid, :sessionUuid::uuid, :seqNum, :prompt,
                            :workflowPath, :workflowParams::jsonb, :workflowId::uuid, :conversationId::uuid,
                            :clipScore, :artifactScore, :vlmScores::jsonb, :vlmIssues, :vlmRecs,
                            :verdict, :rejectionReason, :imagePath, :embedding, :northStarSim
                        )
                        ON CONFLICT (image_uuid) DO UPDATE SET
                            clip_score            = EXCLUDED.clip_score,
                            artifact_score        = EXCLUDED.artifact_score,
                            vlm_scores            = EXCLUDED.vlm_scores,
                            vlm_issues            = EXCLUDED.vlm_issues,
                            vlm_recs              = EXCLUDED.vlm_recs,
                            verdict               = EXCLUDED.verdict,
                            rejection_reason      = EXCLUDED.rejection_reason,
                            embedding             = EXCLUDED.embedding,
                            north_star_similarity = EXCLUDED.north_star_similarity
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
                        .addValue("verdict", finalVerdict)
                        .addValue("rejectionReason", finalRejectionReason)
                        .addValue("imagePath", imagePath)
                        .addValue("embedding", finalEmbedding)
                        .addValue("northStarSim", finalNorthStarSim);

                jdbc.update(sql, params);
                log.info("Stored image " + imageUuid + " verdict=" + finalVerdict);

                if ("candidate".equals(finalVerdict)) {
                    ObjectNode verdictMsg = buildVerdictMessage(
                            imageUuid, sessionUuid, sequenceNumber, prompt,
                            workflowPath, workflowId, conversationId, imagePath,
                            clipScore, aiConfidence, vlmScores, issues, recs, finalNorthStarSim);
                    jmsTemplate.convertAndSend("loop.verdicts", mapper.writeValueAsString(verdictMsg));
                    log.info("Published verdict for " + imageUuid);
                }
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
            return null;
        });
    }

    private ObjectNode buildVerdictMessage(
            String imageUuid, String sessionUuid, int sequenceNumber,
            String prompt, String workflowPath,
            String workflowId, String conversationId, String imagePath,
            double clipScore, double artifactScore, ObjectNode vlmScores,
            String[] issues, String[] recs, Double northStarSimilarity) {
        ObjectNode scores = mapper.createObjectNode()
                .put("clip_score", clipScore)
                .put("artifact_score", artifactScore);
        scores.set("vlm_scores", vlmScores);
        if (northStarSimilarity != null) scores.put("north_star_similarity", northStarSimilarity);

        ObjectNode msg = mapper.createObjectNode();
        msg.put("image_uuid", imageUuid);
        msg.put("verdict", "candidate");
        msg.put("session_uuid", sessionUuid);
        msg.put("sequence_number", sequenceNumber);
        msg.put("prompt", prompt);
        msg.put("workflow_path", workflowPath);
        msg.put("workflow_id", workflowId != null ? workflowId : "");
        msg.put("conversation_id", conversationId != null ? conversationId : "");
        msg.put("image_path", imagePath != null ? imagePath : "");
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
