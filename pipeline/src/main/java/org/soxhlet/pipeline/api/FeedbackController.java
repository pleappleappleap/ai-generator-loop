package org.soxhlet.pipeline.api;

import org.soxhlet.pipeline.service.TasteService;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.UUID;
import java.util.logging.Logger;

@RestController
public class FeedbackController {

    private static final Logger log = Logger.getLogger(FeedbackController.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final TasteService tasteService;

    public FeedbackController(NamedParameterJdbcTemplate jdbc, TasteService tasteService) {
        this.jdbc = jdbc;
        this.tasteService = tasteService;
    }

    @PostMapping("/feedback")
    public Map<String, Object> feedback(@RequestBody Map<String, Object> req) {
        String imageUuid = (String) req.get("image_uuid");
        String workflowId = req.get("workflow_id") != null ? req.get("workflow_id").toString() : null;
        String runId = req.get("run_id") != null ? req.get("run_id").toString() : null;
        String rating = (String) req.get("rating");
        String comment = req.get("comment") != null ? req.get("comment").toString() : null;

        if (imageUuid == null || rating == null) {
            return Map.of("ok", false, "error", "image_uuid and rating required");
        }
        if (runId == null || runId.isBlank()) {
            runId = "00000000-0000-0000-0000-000000000000";
        }

        try {
            jdbc.update(
                    "INSERT INTO user_feedback (image_uuid, workflow_id, session_uuid, rating, comment) " +
                    "VALUES (:imageUuid, :wfId, :sess, :rating, :comment)",
                    new MapSqlParameterSource()
                            .addValue("imageUuid", UUID.fromString(imageUuid))
                            .addValue("wfId", workflowId != null ? UUID.fromString(workflowId) : null)
                            .addValue("sess", UUID.fromString(runId))
                            .addValue("rating", rating)
                            .addValue("comment", comment));
        } catch (Exception e) {
            log.warning("Failed to insert feedback: " + e.getMessage());
            return Map.of("ok", false, "error", e.getMessage());
        }

        // Trigger taste synthesis if threshold reached
        if (workflowId != null && !workflowId.isBlank()) {
            try {
                // Look up conversation_id for this workflow
                var rows = jdbc.queryForList(
                        "SELECT conversation_id FROM workflows WHERE workflow_id = :id",
                        Map.of("id", UUID.fromString(workflowId)), String.class);
                String conversationId = rows.isEmpty() ? null : rows.get(0);
                tasteService.onFeedbackAdded(workflowId, conversationId);
            } catch (Exception e) {
                log.warning("Taste synthesis check failed: " + e.getMessage());
            }
        }

        return Map.of("ok", true);
    }
}
