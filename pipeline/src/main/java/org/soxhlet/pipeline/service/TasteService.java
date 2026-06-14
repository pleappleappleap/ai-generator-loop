package org.soxhlet.pipeline.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.client.RestClient;

import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

/**
 * Accumulates user feedback and synthesises a taste profile via the tactical LLM
 * when the feedback count threshold is reached. Called by FeedbackController (Phase 6).
 */
@Service
public class TasteService {

    private static final Logger log = Logger.getLogger(TasteService.class.getName());

    @Value("${taste.synthesis-threshold:5}")
    private int synthesisThreshold;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final RestClient llmClient;

    public TasteService(
            NamedParameterJdbcTemplate jdbc,
            ObjectMapper mapper,
            @org.springframework.beans.factory.annotation.Qualifier("tacticalLlmClient") RestClient llmClient) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.llmClient = llmClient;
    }

    /**
     * Called after a feedback row is persisted. Checks whether the synthesis
     * threshold has been reached and, if so, runs synthesis.
     */
    @Transactional
    public void onFeedbackAdded(String workflowId, String conversationId) {
        if (workflowId == null || workflowId.isBlank()) return;

        // Count feedback rows since the last synthesis for this workflow.
        List<Map<String, Object>> recent = jdbc.queryForList(
                """
                SELECT uf.rating, uf.comment, i.prompt, i.vlm_scores, i.vlm_issues
                FROM user_feedback uf
                JOIN images i ON i.image_uuid = uf.image_uuid
                WHERE uf.workflow_id = :wfId::uuid
                  AND uf.created_at > COALESCE(
                      (SELECT created_at FROM taste_synthesis
                       WHERE workflow_id = :wfId::uuid ORDER BY id DESC LIMIT 1),
                      '1970-01-01')
                ORDER BY uf.created_at DESC
                """,
                Map.of("wfId", workflowId));

        if (recent.size() < synthesisThreshold) return;

        log.info("Running taste synthesis for workflow " + workflowId + " (" + recent.size() + " feedback rows)");
        String synthesised = runSynthesis(recent);
        if (synthesised == null || synthesised.isBlank()) return;

        jdbc.update(
                """
                INSERT INTO taste_synthesis (conversation_id, workflow_id, content, source_feedback_count)
                VALUES (:convId::uuid, :wfId::uuid, :content, :count)
                """,
                new MapSqlParameterSource()
                        .addValue("convId", conversationId)
                        .addValue("wfId", workflowId)
                        .addValue("content", synthesised)
                        .addValue("count", recent.size()));
        log.info("Taste synthesis written for workflow " + workflowId);
    }

    private String runSynthesis(List<Map<String, Object>> feedbackRows) {
        StringBuilder prompt = new StringBuilder();
        prompt.append("Summarise the user's aesthetic preferences based on this feedback. ");
        prompt.append("Write a concise paragraph (3-5 sentences) describing what they like and dislike. ");
        prompt.append("Focus on concrete, actionable stylistic patterns.\n\n");
        prompt.append("Feedback entries:\n");
        for (Map<String, Object> row : feedbackRows) {
            String rating = (String) row.get("rating");
            String p = (String) row.get("prompt");
            String comment = (String) row.get("comment");
            prompt.append(String.format("  [%s] prompt: %s", rating, p == null ? "" : p.substring(0, Math.min(80, p == null ? 0 : p.length()))));
            if (comment != null && !comment.isBlank()) prompt.append(" | comment: ").append(comment);
            prompt.append("\n");
        }
        prompt.append("\nRespond with the paragraph only - no preamble, no JSON.");

        try {
            ObjectNode messages = mapper.createObjectNode();
            ArrayNode msgArray = mapper.createArrayNode();
            msgArray.add(mapper.createObjectNode().put("role", "user").put("content", prompt.toString()));
            ObjectNode body = mapper.createObjectNode();
            body.put("model", "default");
            body.put("temperature", 0.3);
            body.put("max_tokens", 256);
            body.set("messages", msgArray);

            byte[] bodyBytes = mapper.writeValueAsBytes(body);
            ObjectNode response = llmClient.post()
                    .uri("/chat/completions")
                    .contentType(org.springframework.http.MediaType.APPLICATION_JSON)
                    .body(bodyBytes)
                    .retrieve()
                    .body(ObjectNode.class);

            return response.path("choices").path(0).path("message").path("content").asText("");
        } catch (Exception e) {
            log.warning("Taste synthesis LLM call failed: " + e.getMessage());
            return null;
        }
    }
}
