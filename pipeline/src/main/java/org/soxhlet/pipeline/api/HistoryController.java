package org.soxhlet.pipeline.api;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

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

        List<Map<String, Object>> chatRaw = jdbc.queryForList(
                "SELECT role, content, created_at FROM (" +
                "  SELECT role, content, created_at FROM chat_messages " +
                "  WHERE conversation_id = :convId " +
                "  ORDER BY created_at DESC LIMIT :limit" +
                ") sub ORDER BY created_at ASC",
                Map.of("convId", UUID.fromString(conversationId), "limit", limit));

        List<Map<String, Object>> feedbackRaw;
        if (!workflowId.isBlank()) {
            feedbackRaw = jdbc.queryForList(
                    "SELECT uf.rating, uf.comment, uf.image_uuid, uf.created_at " +
                    "FROM user_feedback uf " +
                    "WHERE uf.workflow_id = :wfId " +
                    "ORDER BY uf.created_at DESC LIMIT 20",
                    Map.of("wfId", UUID.fromString(workflowId)));
        } else {
            feedbackRaw = jdbc.queryForList(
                    "SELECT uf.rating, uf.comment, uf.image_uuid, uf.created_at " +
                    "FROM user_feedback uf " +
                    "JOIN images i ON i.image_uuid = uf.image_uuid " +
                    "WHERE i.conversation_id = :convId " +
                    "ORDER BY uf.created_at DESC LIMIT 20",
                    Map.of("convId", UUID.fromString(conversationId)));
        }

        return Map.of(
                "ok", true,
                "chat", toStringDates(chatRaw),
                "feedback", toStringDates(feedbackRaw));
    }

    private static List<Map<String, Object>> toStringDates(List<Map<String, Object>> rows) {
        List<Map<String, Object>> out = new ArrayList<>(rows.size());
        for (Map<String, Object> row : rows) {
            Map<String, Object> m = new HashMap<>(row);
            for (String key : m.keySet()) {
                Object v = m.get(key);
                if (v != null && !(v instanceof String) && !(v instanceof Number) && !(v instanceof Boolean)) {
                    m.put(key, v.toString());
                }
            }
            out.add(m);
        }
        return out;
    }
}
