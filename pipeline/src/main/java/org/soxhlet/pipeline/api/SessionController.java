package org.soxhlet.pipeline.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.soxhlet.pipeline.service.ContextService;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;

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

    @GetMapping("/conversations")
    public List<Map<String, Object>> listConversations() {
        List<Map<String, Object>> convRows = jdbc.queryForList(
                "SELECT conversation_id AS id, name FROM conversations " +
                "WHERE status != 'archived' ORDER BY created_at DESC LIMIT 100",
                Map.of());
        if (convRows.isEmpty()) return List.of();

        Map<String, Map<String, Object>> byId = new LinkedHashMap<>();
        for (Map<String, Object> r : convRows) {
            String id = r.get("id").toString();
            Map<String, Object> conv = new HashMap<>();
            conv.put("conversation_id", id);
            conv.put("name", r.get("name"));
            conv.put("workflows", new ArrayList<>());
            byId.put(id, conv);
        }

        List<UUID> uuidIds = byId.keySet().stream().map(UUID::fromString).collect(Collectors.toList());
        List<Map<String, Object>> wfRows = jdbc.queryForList(
                "SELECT workflow_id AS wid, conversation_id AS cid, " +
                "name, COALESCE(workflow_path, '') AS workflow_path " +
                "FROM workflows WHERE conversation_id IN (:ids) " +
                "ORDER BY created_at ASC",
                new MapSqlParameterSource("ids", uuidIds));
        for (Map<String, Object> wf : wfRows) {
            String cid = wf.get("cid").toString();
            Map<String, Object> conv = byId.get(cid);
            if (conv == null) continue;
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> wfList = (List<Map<String, Object>>) conv.get("workflows");
            Map<String, Object> wfEntry = new HashMap<>();
            wfEntry.put("workflow_id", wf.get("wid").toString());
            wfEntry.put("name", wf.get("name"));
            wfEntry.put("workflow_path", wf.get("workflow_path"));
            wfEntry.put("conversation_id", cid);
            wfList.add(wfEntry);
        }
        return new ArrayList<>(byId.values());
    }

    @PostMapping("/session/start")
    public Map<String, Object> sessionStart(@RequestBody Map<String, Object> req) {
        String name = req.get("name") != null ? req.get("name").toString() : "Untitled";
        if (name.isBlank()) name = "Untitled";

        List<String> ids = jdbc.queryForList(
                "INSERT INTO conversations (name) VALUES (:name) RETURNING conversation_id",
                Map.of("name", name), String.class);

        if (ids.isEmpty()) {
            return Map.of("ok", false, "error", "failed to create conversation");
        }
        return Map.of("ok", true, "conversation_id", ids.get(0));
    }

    @DeleteMapping("/conversations/{conversationId}")
    public Map<String, Object> deleteConversation(@PathVariable String conversationId) {
        int updated = jdbc.update(
                "UPDATE conversations SET status = 'archived' WHERE conversation_id = :id",
                Map.of("id", UUID.fromString(conversationId)));
        if (updated == 0) return Map.of("ok", false, "error", "conversation not found");
        return Map.of("ok", true);
    }

    @PostMapping("/conversations/{conversationId}/cancel")
    public Map<String, Object> cancelConversation(@PathVariable String conversationId) {
        int updated = jdbc.update(
                "UPDATE conversations SET status = 'cancelled' " +
                "WHERE conversation_id = :id",
                Map.of("id", UUID.fromString(conversationId)));
        if (updated == 0) return Map.of("ok", false, "error", "conversation not found");
        return Map.of("ok", true);
    }

    @PatchMapping("/conversations/{conversationId}")
    public Map<String, Object> renameConversation(
            @PathVariable String conversationId,
            @RequestBody Map<String, Object> req) {
        String name = req.get("name") != null ? req.get("name").toString().strip() : "";
        if (name.isBlank()) return Map.of("ok", false, "error", "name is required");
        int updated = jdbc.update(
                "UPDATE conversations SET name = :name WHERE conversation_id = :id",
                Map.of("name", name, "id", UUID.fromString(conversationId)));
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
