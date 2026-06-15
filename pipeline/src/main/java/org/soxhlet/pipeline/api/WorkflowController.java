package org.soxhlet.pipeline.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Logger;

@RestController
public class WorkflowController {

    private static final Logger log = Logger.getLogger(WorkflowController.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final JmsTemplate jmsTemplate;
    private final ObjectMapper mapper;

    @Value("${ui.default-workflow-path:loop/workflows/sdxl_base.json}")
    private String defaultWorkflowPath;

    public WorkflowController(NamedParameterJdbcTemplate jdbc,
                               JmsTemplate jmsTemplate,
                               ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
    }

    // -- POST /workflow/new -----------------------------------------------------

    @PostMapping("/workflow/new")
    public Map<String, Object> workflowNew(@RequestBody Map<String, Object> req) {
        String conversationId = (String) req.get("conversation_id");
        String name = req.getOrDefault("name", "Untitled Workflow").toString();
        String workflowPath = (String) req.get("workflow_path");
        if (workflowPath == null || workflowPath.isBlank()) workflowPath = defaultWorkflowPath;
        String description = req.get("description") != null ? req.get("description").toString() : null;
        String budgetPolicy = req.getOrDefault("budget_policy", "fresh").toString();
        int maxRetries = req.get("max_retries") != null ? ((Number) req.get("max_retries")).intValue() : 3;
        int maxInpaints = req.get("max_inpaints") != null ? ((Number) req.get("max_inpaints")).intValue() : 2;

        if (conversationId == null) {
            return Map.of("ok", false, "error", "conversation_id required");
        }

        List<String> wfIds = jdbc.queryForList(
                "INSERT INTO workflows (conversation_id, name, workflow_path, description, " +
                "budget_policy, max_retries, max_inpaints) " +
                "VALUES (:convId, :name, :path, :desc, :policy, :maxR, :maxI) " +
                "RETURNING workflow_id",
                new MapSqlParameterSource()
                        .addValue("convId", UUID.fromString(conversationId))
                        .addValue("name", name)
                        .addValue("path", workflowPath)
                        .addValue("desc", description)
                        .addValue("policy", budgetPolicy)
                        .addValue("maxR", maxRetries)
                        .addValue("maxI", maxInpaints),
                String.class);

        if (wfIds.isEmpty()) {
            return Map.of("ok", false, "error", "failed to create workflow");
        }
        String workflowId = wfIds.get(0);

        String runId = insertBudget(workflowId, conversationId, maxRetries, maxInpaints);

        return Map.of(
                "ok", true,
                "workflow_id", workflowId,
                "run_id", runId,
                "conversation_id", conversationId,
                "workflow_path", workflowPath);
    }

    // -- POST /workflow/{id}/resume ---------------------------------------------

    @PostMapping("/workflow/{workflowId}/resume")
    public Map<String, Object> workflowResume(
            @PathVariable String workflowId,
            @RequestBody(required = false) Map<String, Object> req) {

        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT conversation_id, workflow_path, budget_policy, " +
                "max_retries, max_inpaints FROM workflows WHERE workflow_id = :id",
                Map.of("id", UUID.fromString(workflowId)));

        if (rows.isEmpty()) {
            return Map.of("ok", false, "error", "workflow not found");
        }
        Map<String, Object> wf = rows.get(0);

        String conversationId = wf.get("conversation_id").toString();
        String workflowPath = (String) wf.get("workflow_path");
        String budgetPolicy = (String) wf.get("budget_policy");
        int maxRetries = ((Number) wf.get("max_retries")).intValue();
        int maxInpaints = ((Number) wf.get("max_inpaints")).intValue();

        if (req != null) {
            if (req.get("budget_policy") != null) budgetPolicy = req.get("budget_policy").toString();
            if (req.get("max_retries") != null) maxRetries = ((Number) req.get("max_retries")).intValue();
            if (req.get("max_inpaints") != null) maxInpaints = ((Number) req.get("max_inpaints")).intValue();
        }

        int effectiveRetries = maxRetries;
        int effectiveInpaints = maxInpaints;

        if ("accumulated".equals(budgetPolicy)) {
            List<Map<String, Object>> last = jdbc.queryForList(
                    "SELECT retries_used, inpaints_used FROM budget " +
                    "WHERE workflow_id = :wfId ORDER BY created_at DESC LIMIT 1",
                    Map.of("wfId", UUID.fromString(workflowId)));
            if (!last.isEmpty()) {
                int prevRetries = ((Number) last.get(0).get("retries_used")).intValue();
                int prevInpaints = ((Number) last.get(0).get("inpaints_used")).intValue();
                effectiveRetries = Math.max(0, maxRetries - prevRetries);
                effectiveInpaints = Math.max(0, maxInpaints - prevInpaints);
            }
        }

        String runId = insertBudget(workflowId, conversationId, effectiveRetries, effectiveInpaints);

        return Map.of(
                "ok", true,
                "run_id", runId,
                "conversation_id", conversationId,
                "workflow_path", workflowPath);
    }

    // -- POST /workflow/{id}/start-run ------------------------------------------

    @PostMapping("/workflow/{workflowId}/start-run")
    public Map<String, Object> workflowStartRun(
            @PathVariable String workflowId,
            @RequestBody Map<String, Object> req) {

        String prompt = (String) req.get("prompt");
        String runId = (String) req.get("run_id");
        String conversationId = (String) req.get("conversation_id");
        String workflowPath = (String) req.get("workflow_path");

        if (runId == null) {
            return Map.of("ok", false, "error", "run_id required");
        }
        if (prompt == null) prompt = "";

        String imageUuid = UUID.randomUUID().toString();

        try {
            ObjectNode genMsg = mapper.createObjectNode();
            genMsg.put("image_uuid", imageUuid);
            genMsg.put("session_uuid", runId);
            genMsg.put("sequence_number", 0);
            genMsg.put("workflow_path", workflowPath != null ? workflowPath : "");
            genMsg.put("workflow_id", workflowId);
            genMsg.put("conversation_id", conversationId != null ? conversationId : "");
            genMsg.put("prompt", prompt);

            @SuppressWarnings("unchecked")
            Map<String, Object> workflowParams = req.get("workflow_params") instanceof Map
                    ? (Map<String, Object>) req.get("workflow_params") : Map.of();
            genMsg.set("workflow_params", mapper.valueToTree(workflowParams));
            genMsg.set("loras", mapper.createArrayNode());

            jmsTemplate.convertAndSend("loop.generate", mapper.writeValueAsString(genMsg));
            log.info("Queued generation " + imageUuid + " for workflow " + workflowId);
            return Map.of("ok", true, "image_uuid", imageUuid);
        } catch (Exception e) {
            log.warning("Failed to queue generation: " + e.getMessage());
            return Map.of("ok", false, "error", e.getMessage());
        }
    }

    // -- Budget helper ----------------------------------------------------------

    private String insertBudget(String workflowId, String conversationId,
                                 int maxRetries, int maxInpaints) {
        String sessionUuid = UUID.randomUUID().toString();
        jdbc.update(
                "INSERT INTO budget (session_uuid, workflow_id, conversation_id, " +
                "max_retries, max_inpaints, expires_at) " +
                "VALUES (:sess, :wfId, :convId, " +
                ":maxR, :maxI, now() + interval '24 hours')",
                new MapSqlParameterSource()
                        .addValue("sess", UUID.fromString(sessionUuid))
                        .addValue("wfId", UUID.fromString(workflowId))
                        .addValue("convId", conversationId != null ? UUID.fromString(conversationId) : null)
                        .addValue("maxR", maxRetries)
                        .addValue("maxI", maxInpaints));
        return sessionUuid;
    }
}
