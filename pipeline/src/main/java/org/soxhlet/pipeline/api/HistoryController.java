package org.soxhlet.pipeline.api;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@RestController
public class HistoryController {

    private final NamedParameterJdbcTemplate jdbc;

    public HistoryController(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @GetMapping("/history")
    public Map<String, Object> history(
            @RequestParam(name = "conversation_id", defaultValue = "") String conversationId,
            @RequestParam(name = "workflow_id", defaultValue = "") String workflowId,
            @RequestParam(name = "limit", defaultValue = "50") int limit) {

        if (conversationId.isBlank()) {
            return Map.of("ok", true, "chat", List.of(), "feedback", List.of());
        }

        // Chat messages - all for the conversation, or workflow-scoped if provided
        List<Map<String, Object>> chatRaw = jdbc.queryForList(
                "SELECT role, content, created_at::text FROM (" +
                "  SELECT role, content, created_at FROM chat_messages " +
                "  WHERE conversation_id = :convId::uuid " +
                "  ORDER BY created_at DESC LIMIT :limit" +
                ") sub ORDER BY created_at ASC",
                Map.of("convId", conversationId, "limit", limit));

        // Recent user feedback for this workflow (or conversation)
        List<Map<String, Object>> feedbackRaw;
        if (!workflowId.isBlank()) {
            feedbackRaw = jdbc.queryForList(
                    "SELECT uf.rating, uf.comment, uf.image_uuid::text, uf.created_at::text " +
                    "FROM user_feedback uf " +
                    "WHERE uf.workflow_id = :wfId::uuid " +
                    "ORDER BY uf.created_at DESC LIMIT 20",
                    Map.of("wfId", workflowId));
        } else {
            feedbackRaw = jdbc.queryForList(
                    "SELECT uf.rating, uf.comment, uf.image_uuid::text, uf.created_at::text " +
                    "FROM user_feedback uf " +
                    "JOIN images i ON i.image_uuid = uf.image_uuid " +
                    "WHERE i.conversation_id = :convId::uuid " +
                    "ORDER BY uf.created_at DESC LIMIT 20",
                    Map.of("convId", conversationId));
        }

        return Map.of(
                "ok", true,
                "chat", chatRaw,
                "feedback", feedbackRaw);
    }
}
