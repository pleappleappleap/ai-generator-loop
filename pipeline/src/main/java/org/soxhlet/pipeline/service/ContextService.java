package org.soxhlet.pipeline.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

@Service
public class ContextService {

    private static final Logger log = Logger.getLogger(ContextService.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final RestClient clipClient;

    public ContextService(
            NamedParameterJdbcTemplate jdbc,
            @Qualifier("clipScorerClient") RestClient clipClient) {
        this.jdbc = jdbc;
        this.clipClient = clipClient;
    }

    public String northStar() {
        List<String> rows = jdbc.queryForList(
                "SELECT content FROM north_star WHERE superseded_at IS NULL " +
                "ORDER BY id DESC LIMIT 1",
                Map.of(), String.class);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public String sessionDirection(String workflowId) {
        if (workflowId == null || workflowId.isBlank()) return null;
        List<String> rows = jdbc.queryForList(
                "SELECT content FROM session_direction " +
                "WHERE workflow_id = :wfId AND superseded_at IS NULL " +
                "ORDER BY id DESC LIMIT 1",
                Map.of("wfId", java.util.UUID.fromString(workflowId)), String.class);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public String tasteSynthesis(String workflowId) {
        if (workflowId == null || workflowId.isBlank()) return null;
        List<String> rows = jdbc.queryForList(
                "SELECT content FROM taste_synthesis " +
                "WHERE workflow_id = :wfId " +
                "ORDER BY id DESC LIMIT 1",
                Map.of("wfId", java.util.UUID.fromString(workflowId)), String.class);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public void writeNorthStar(String content, String createdBy) {
        String embeddingStr = null;
        try {
            JsonNode result = clipClient.post().uri("/embed_text")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(Map.of("text", content))
                    .retrieve()
                    .body(JsonNode.class);
            if (result != null && result.has("embedding")) {
                embeddingStr = result.get("embedding").toString();
            }
        } catch (Exception e) {
            log.warning("CLIP /embed_text failed for north_star - storing without embedding: " + e.getMessage());
        }

        jdbc.update("UPDATE north_star SET superseded_at = now() WHERE superseded_at IS NULL", Map.of());

        if (embeddingStr != null) {
            jdbc.update(
                    "INSERT INTO north_star (content, embedding, created_by) " +
                    "VALUES (:content, :embedding, :createdBy)",
                    Map.of("content", content, "embedding", embeddingStr, "createdBy", createdBy));
        } else {
            jdbc.update(
                    "INSERT INTO north_star (content, created_by) VALUES (:content, :createdBy)",
                    Map.of("content", content, "createdBy", createdBy));
        }
    }
}
