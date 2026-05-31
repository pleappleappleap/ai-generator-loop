package org.soxhlet.pipeline.api;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
public class SessionController {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public SessionController(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @PostMapping("/session/start")
    public Map<String, Object> sessionStart(@RequestBody Map<String, Object> req) {
        String name = req.get("name") != null ? req.get("name").toString() : "Untitled";
        if (name.isBlank()) name = "Untitled";

        List<String> ids = jdbc.queryForList(
                "INSERT INTO conversations (name) VALUES (:name) RETURNING conversation_id::text",
                Map.of("name", name), String.class);

        if (ids.isEmpty()) {
            return Map.of("ok", false, "error", "failed to create conversation");
        }
        return Map.of("ok", true, "conversation_id", ids.get(0));
    }

    @PostMapping("/conversations/{conversationId}/cancel")
    public Map<String, Object> cancelConversation(@PathVariable String conversationId) {
        int updated = jdbc.update(
                "UPDATE conversations SET status = 'cancelled' " +
                "WHERE conversation_id = :id::uuid",
                Map.of("id", conversationId));
        if (updated == 0) return Map.of("ok", false, "error", "conversation not found");
        return Map.of("ok", true);
    }
}
