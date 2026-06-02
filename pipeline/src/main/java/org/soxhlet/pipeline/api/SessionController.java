package org.soxhlet.pipeline.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.soxhlet.pipeline.service.ContextService;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
public class SessionController {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ContextService contextService;

    public SessionController(
            NamedParameterJdbcTemplate jdbc,
            ObjectMapper mapper,
            ContextService contextService) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.contextService = contextService;
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

    @DeleteMapping("/conversations/{conversationId}")
    public Map<String, Object> deleteConversation(@PathVariable String conversationId) {
        int updated = jdbc.update(
                "UPDATE conversations SET status = 'archived' WHERE conversation_id = :id::uuid",
                Map.of("id", conversationId));
        if (updated == 0) return Map.of("ok", false, "error", "conversation not found");
        return Map.of("ok", true);
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

    @PostMapping("/north-star")
    public Map<String, Object> writeNorthStar(@RequestBody Map<String, Object> req) {
        String content = req.get("content") != null ? req.get("content").toString() : "";
        if (content.isBlank()) return Map.of("ok", false, "error", "content is required");
        contextService.writeNorthStar(content, "user");
        return Map.of("ok", true);
    }
}
