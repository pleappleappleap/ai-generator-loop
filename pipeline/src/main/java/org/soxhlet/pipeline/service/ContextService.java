package org.soxhlet.pipeline.service;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

@Service
public class ContextService {

    private final NamedParameterJdbcTemplate jdbc;

    public ContextService(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
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
                "WHERE workflow_id = :wfId::uuid AND superseded_at IS NULL " +
                "ORDER BY id DESC LIMIT 1",
                Map.of("wfId", workflowId), String.class);
        return rows.isEmpty() ? null : rows.get(0);
    }

    public String tasteSynthesis(String workflowId) {
        if (workflowId == null || workflowId.isBlank()) return null;
        List<String> rows = jdbc.queryForList(
                "SELECT content FROM taste_synthesis " +
                "WHERE workflow_id = :wfId::uuid " +
                "ORDER BY id DESC LIMIT 1",
                Map.of("wfId", workflowId), String.class);
        return rows.isEmpty() ? null : rows.get(0);
    }
}
