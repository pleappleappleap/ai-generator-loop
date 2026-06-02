package org.soxhlet.pipeline.worker;

import org.soxhlet.pipeline.config.TacticalLlmConfig;
import org.soxhlet.pipeline.service.ChatBroadcastService;
import org.soxhlet.pipeline.service.ContextService;
import org.soxhlet.pipeline.service.GalleryBroadcastService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ApplicationContext;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.client.RestClient;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Logger;
import java.util.stream.Collectors;

@Component
public class TacticalLlmCaller {

    private static final Logger log = Logger.getLogger(TacticalLlmCaller.class.getName());

    private final TransactionTemplate requiresNew;
    private final NamedParameterJdbcTemplate jdbc;
    private final JmsTemplate jmsTemplate;
    private final ObjectMapper mapper;
    private final TacticalLlmConfig cfg;
    private final ContextService contextService;
    private final RestClient llmClient;
    private final RestClient vlmClient;
    private final RestClient comfyuiClient;
    private final GalleryBroadcastService galleryBroadcast;
    private final ChatBroadcastService chatBroadcast;
    private final ApplicationContext applicationContext;
    private final Path systemPromptPath;
    private final String resolvedModelId;
    private final Path workflowsDir;
    private final Path comfyuiModelsDir;

    // conversationId → {workflow_id, session_uuid, workflow_path}
    private final ConcurrentHashMap<String, Map<String, String>> conversationWorkflows
            = new ConcurrentHashMap<>();

    public TacticalLlmCaller(
            PlatformTransactionManager txManager,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            TacticalLlmConfig cfg,
            ContextService contextService,
            GalleryBroadcastService galleryBroadcast,
            ChatBroadcastService chatBroadcast,
            ApplicationContext applicationContext,
            @Qualifier("tacticalLlmClient") RestClient llmClient,
            @Qualifier("vlmScorerClient") RestClient vlmClient,
            @Qualifier("comfyuiClient") RestClient comfyuiClient,
            @Value("${tactical-llm.system-prompt-file:loop/tactical-llm/system_prompt.md}") String systemPromptFile,
            @Value("${ui.workflows-dir:loop/workflows}") String workflowsDirStr,
            @Value("${comfyui.models-dir:loop/ComfyUI/models}") String comfyuiModelsDirStr) {
        this.requiresNew = new TransactionTemplate(txManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
        this.cfg = cfg;
        this.contextService = contextService;
        this.galleryBroadcast = galleryBroadcast;
        this.chatBroadcast = chatBroadcast;
        this.applicationContext = applicationContext;
        this.llmClient = llmClient;
        this.vlmClient = vlmClient;
        this.comfyuiClient = comfyuiClient;
        this.workflowsDir = Path.of(workflowsDirStr).toAbsolutePath().normalize();
        this.comfyuiModelsDir = Path.of(comfyuiModelsDirStr).toAbsolutePath().normalize();
        this.systemPromptPath = Path.of(systemPromptFile).toAbsolutePath().normalize();
        if (!Files.exists(this.systemPromptPath)) {
            throw new IllegalStateException(
                    "Cannot find tactical LLM system prompt at '" + this.systemPromptPath +
                    "'. Set tactical-llm.system-prompt-file to an existing file path.");
        }
        this.resolvedModelId = resolveModelId(cfg.getModel(), llmClient, mapper);
    }

    // ── Entry point from ChatWsHandler ─────────────────────────────────────────

    public void handleUserMessage(String conversationId, WebSocketSession session) {
        try {
            List<ObjectNode> messages = buildConversation(conversationId);
            JsonNode decision = runAgenticLoop(messages, false);

            if (decision == null) {
                sendJson(session, jsonError("The agent did not respond."));
                return;
            }

            String message = nullIfBlank(decision.path("message").asText(""));
            String action = decision.path("decision").asText("await_input");

            handleVlmEvalPrompt(conversationId, decision);

            if ("generate".equals(action)) {
                Map<String, String> state = queueNewGeneration(conversationId, decision);
                if (state != null) {
                    if (message == null) message = "Generation submitted — watching for results.";
                    galleryBroadcast.decision("", "generate", "", 1.0,
                            state.get("workflow_id"), conversationId);
                }
            }

            if (message != null) {
                saveChatMessage(conversationId, null, "assistant", message);
                sendJson(session, jsonChunk(message));
            }
            sendJson(session, jsonDone());

        } catch (Exception e) {
            log.warning("handleUserMessage error for " + conversationId + ": " + e.getMessage());
            try { sendJson(session, jsonError("Agent error: " + e.getMessage())); } catch (IOException ignored) {}
        }
    }

    // ── JMS listener — stage only ──────────────────────────────────────────────

    @Transactional
    @JmsListener(destination = "loop.verdicts")
    public void onVerdict(String body) throws Exception {
        JsonNode msg = mapper.readTree(body);
        jdbc.update(
                "INSERT INTO pending_decisions (image_uuid, session_uuid, verdict_payload) " +
                "VALUES (:id::uuid, :sess::uuid, :payload::jsonb) ON CONFLICT DO NOTHING",
                Map.of(
                        "id", msg.get("image_uuid").asText(),
                        "sess", msg.get("session_uuid").asText(),
                        "payload", body));
    }

    // ── Polling loop ───────────────────────────────────────────────────────────

    @Scheduled(fixedDelay = 2000)
    public void pollAndProcess() {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT image_uuid::text, verdict_payload::text, decision " +
                "FROM pending_decisions LIMIT 1",
                Map.of());
        if (rows.isEmpty()) return;
        Map<String, Object> row = rows.get(0);
        String imageUuid   = (String) row.get("image_uuid");
        String decision    = (String) row.get("decision");
        String payloadJson = (String) row.get("verdict_payload");
        try {
            JsonNode msg = mapper.readTree(payloadJson);
            if (decision != null) {
                completeTx2(imageUuid, msg, decision, null);
            } else {
                processVerdict(imageUuid, msg);
            }
        } catch (Exception e) {
            log.warning("Decision processing failed for " + imageUuid + ": " + e.getMessage());
        }
    }

    // ── Core verdict processing ────────────────────────────────────────────────

    private void processVerdict(String imageUuid, JsonNode msg) throws Exception {
        String sessionUuid    = msg.path("session_uuid").asText();
        String workflowId     = nullIfBlank(msg.path("workflow_id").asText(""));
        String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));

        Budget budget = loadBudget(sessionUuid);

        if (conversationId != null) {
            List<String> status = jdbc.queryForList(
                    "SELECT status FROM conversations WHERE conversation_id = :id::uuid",
                    Map.of("id", conversationId), String.class);
            if (!status.isEmpty() && "cancelled".equals(status.get(0))) {
                log.info("Conversation " + conversationId + " cancelled — giving up for " + imageUuid);
                completeTx2(imageUuid, msg, "give_up", null);
                return;
            }
        }

        // Append scoring context to conversation history before calling LLM
        String scoringMsg = buildScoringMessage(imageUuid, msg, budget);
        if (conversationId != null) {
            saveChatMessage(conversationId, workflowId, "user", scoringMsg);
        }

        boolean thinkFirst = shouldThink(msg, budget);
        List<ObjectNode> messages = buildConversation(conversationId, scoringMsg);
        JsonNode decision = runAgenticLoop(messages, thinkFirst);

        if (decision != null && "escalate".equals(decision.path("decision").asText("")) && !thinkFirst) {
            log.info("First-pass escalate with thinking=off — retrying with thinking for " + imageUuid);
            JsonNode secondPass = runAgenticLoop(buildConversation(conversationId, null), true);
            if (secondPass != null) decision = secondPass;
        }

        if (decision == null || !decision.has("decision")) {
            decision = heuristicDecision(msg, budget);
        }

        String action = decision.path("decision").asText("give_up");
        String message = nullIfBlank(decision.path("message").asText(""));

        handleVlmEvalPrompt(conversationId, decision);

        if (message != null && conversationId != null) {
            saveChatMessage(conversationId, workflowId, "assistant", message);
            chatBroadcast.sendMessage(conversationId, message);
        }

        final String finalAction = action;
        requiresNew.execute(status -> {
            jdbc.update(
                    "UPDATE pending_decisions SET decision = :decision WHERE image_uuid = :id::uuid",
                    Map.of("decision", finalAction, "id", imageUuid));
            return null;
        });

        completeTx2(imageUuid, msg, action, decision);

        if ("escalate".equals(action)) {
            initiateShutdown();
            return;
        }

        double confidence = decision.path("confidence").asDouble(0.0);
        String reasoning  = decision.path("reasoning").asText("");
        galleryBroadcast.decision(imageUuid, action, reasoning, confidence, workflowId, conversationId);
    }

    private void completeTx2(String imageUuid, JsonNode msg, String action, JsonNode decision) {
        requiresNew.execute(status -> {
            try {
                jdbc.update("DELETE FROM pending_decisions WHERE image_uuid = :id::uuid",
                        Map.of("id", imageUuid));

                String sessionUuid    = msg.path("session_uuid").asText();
                String workflowId     = nullIfBlank(msg.path("workflow_id").asText(""));
                String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));

                jdbc.update(
                        "UPDATE images SET decision = :decision WHERE image_uuid = :id::uuid",
                        Map.of("decision", action, "id", imageUuid));

                switch (action) {
                    case "accept" -> log.info("Accepted " + imageUuid);

                    case "retry", "inpaint", "generate" -> {
                        if ("inpaint".equals(action)) {
                            log.info("Inpaint not implemented — downgrading to retry for " + imageUuid);
                        }

                        Budget budget = loadBudget(sessionUuid);
                        if (budget.retriesUsed >= budget.maxRetries) {
                            jdbc.update("UPDATE images SET decision = 'give_up' WHERE image_uuid = :id::uuid",
                                    Map.of("id", imageUuid));
                            log.info("Retry budget exhausted — giving up for " + imageUuid);
                            break;
                        }

                        incrementBudget(sessionUuid, "retries_used");

                        String newImageUuid  = UUID.randomUUID().toString();
                        int    newSeq        = msg.path("sequence_number").asInt(0) + 1;
                        String retryPrompt   = decision != null
                                ? decision.path("retry_prompt").asText(msg.path("prompt").asText(""))
                                : msg.path("prompt").asText("");
                        JsonNode retryParams = decision != null && decision.has("retry_params")
                                ? decision.get("retry_params") : mapper.createObjectNode();
                        JsonNode retryGraphOps = decision != null && decision.has("retry_graph_ops")
                                ? decision.get("retry_graph_ops") : mapper.createArrayNode();
                        JsonNode retryLoras  = decision != null && decision.has("retry_loras")
                                ? decision.get("retry_loras") : mapper.createArrayNode();
                        String retryModel    = decision != null
                                ? nullIfBlank(decision.path("retry_model").asText("")) : null;

                        ObjectNode baseParams   = (ObjectNode) msg.path("scores").path("workflow_params");
                        ObjectNode mergedParams = baseParams != null && !baseParams.isMissingNode()
                                ? baseParams.deepCopy() : mapper.createObjectNode();
                        if (retryParams.isObject()) {
                            retryParams.fields().forEachRemaining(e -> mergedParams.set(e.getKey(), e.getValue()));
                        }

                        ObjectNode retryMsg = mapper.createObjectNode();
                        retryMsg.put("image_uuid", newImageUuid);
                        retryMsg.put("session_uuid", sessionUuid);
                        retryMsg.put("sequence_number", newSeq);
                        retryMsg.put("workflow_path", msg.path("workflow_path").asText(""));
                        retryMsg.put("prompt", retryPrompt);
                        retryMsg.put("workflow_id", workflowId != null ? workflowId : "");
                        retryMsg.put("conversation_id", conversationId != null ? conversationId : "");
                        if (retryModel != null) retryMsg.put("model_type", retryModel);
                        retryMsg.set("workflow_params", mergedParams);
                        retryMsg.set("graph_ops", retryGraphOps);
                        retryMsg.set("loras", retryLoras);

                        jmsTemplate.convertAndSend("loop.retry", mapper.writeValueAsString(retryMsg));
                        log.info("Retry queued: " + imageUuid + " → " + newImageUuid + " seq=" + newSeq);
                    }

                    case "give_up" -> {
                        String reasoning = decision != null ? decision.path("reasoning").asText("") : "unknown";
                        log.info("Give up for " + imageUuid + ": " + reasoning);
                    }

                    case "escalate" -> {
                        String reason = decision != null ? decision.path("reasoning").asText("") : "";
                        jdbc.update(
                                "INSERT INTO pipeline_events (type, reason) VALUES ('mode_switch_requested', :reason)",
                                Map.of("reason", reason));
                        log.info("Escalation recorded for " + imageUuid + ": " + reason);
                    }
                }
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
            return null;
        });
    }

    // ── Conversation building ──────────────────────────────────────────────────

    private List<ObjectNode> buildConversation(String conversationId) {
        return buildConversation(conversationId, null);
    }

    private List<ObjectNode> buildConversation(String conversationId, String extraUserMessage) {
        String contextualSystem = readSystemPrompt() + "\n\n" + buildContextBlock(conversationId);

        List<ObjectNode> messages = new ArrayList<>();
        messages.add(mapper.createObjectNode().put("role", "system").put("content", contextualSystem));

        if (conversationId != null) {
            List<Map<String, Object>> rows = jdbc.queryForList(
                    "SELECT role, content FROM (" +
                    "  SELECT role, content, created_at FROM chat_messages " +
                    "  WHERE conversation_id = :convId::uuid " +
                    "  ORDER BY created_at DESC LIMIT 60" +
                    ") sub ORDER BY created_at ASC",
                    Map.of("convId", conversationId));
            for (Map<String, Object> row : rows) {
                messages.add(mapper.createObjectNode()
                        .put("role", (String) row.get("role"))
                        .put("content", (String) row.get("content")));
            }
        }

        if (extraUserMessage != null) {
            messages.add(mapper.createObjectNode().put("role", "user").put("content", extraUserMessage));
        }

        return messages;
    }

    private String buildContextBlock(String conversationId) {
        StringBuilder sb = new StringBuilder("═══ CURRENT CONTEXT ═════════════════════════════════════════════\n");

        String northStar = contextService.northStar();
        if (northStar != null) {
            sb.append("North star (long-term artistic vision):\n").append(northStar).append("\n\n");
        }

        String workflowId = null;
        Map<String, String> wfState = conversationId != null ? conversationWorkflows.get(conversationId) : null;
        if (wfState != null) workflowId = wfState.get("workflow_id");

        String sessionDir = contextService.sessionDirection(workflowId);
        if (sessionDir != null) {
            sb.append("Session direction:\n").append(sessionDir).append("\n\n");
        }

        String taste = contextService.tasteSynthesis(workflowId);
        if (taste != null) {
            sb.append("Taste synthesis:\n").append(taste).append("\n\n");
        }

        List<String> workflows = listWorkflowNames();
        sb.append("Available workflows: ").append(
                workflows.isEmpty() ? "(none in loop/workflows/)" : String.join(", ", workflows)
        ).append("\n");

        return sb.toString();
    }

    // ── New generation (user message path) ────────────────────────────────────

    private Map<String, String> queueNewGeneration(String conversationId, JsonNode decision) {
        try {
            String workflowName = nullIfBlank(decision.path("generate_workflow").asText(""));
            if (workflowName == null) {
                List<String> available = listWorkflowNames();
                if (available.isEmpty()) {
                    log.warning("No workflows available for generation");
                    return null;
                }
                workflowName = available.get(0);
            }

            Path workflowPath = workflowsDir.resolve(workflowName);
            if (!Files.exists(workflowPath)) {
                log.warning("Workflow not found: " + workflowPath);
                return null;
            }

            String prompt      = decision.path("generate_prompt").asText("").strip();
            String modelType   = decision.path("generate_model").asText("auto");
            JsonNode params    = decision.has("generate_params") ? decision.get("generate_params") : mapper.createObjectNode();
            JsonNode loras     = decision.has("generate_loras") ? decision.get("generate_loras") : mapper.createArrayNode();
            JsonNode graphOps  = decision.has("generate_graph_ops") ? decision.get("generate_graph_ops") : mapper.createArrayNode();

            // Reuse existing workflow state if workflow path matches
            Map<String, String> state = conversationWorkflows.get(conversationId);
            if (state == null || !workflowPath.toString().equals(state.get("workflow_path"))) {
                String wfLabel = workflowName.replace(".json", "").replace("_", " ");
                wfLabel = Character.toUpperCase(wfLabel.charAt(0)) + wfLabel.substring(1);

                String workflowId = createWorkflow(conversationId, wfLabel, workflowPath.toString(), 3, 2);
                if (workflowId == null) return null;

                String sessionUuid = UUID.randomUUID().toString();
                jdbc.update(
                        "INSERT INTO budget (session_uuid, workflow_id, conversation_id, " +
                        "max_retries, max_inpaints, expires_at) " +
                        "VALUES (:sess::uuid, :wfId::uuid, :convId::uuid, :maxRetries, :maxInpaints, " +
                        "now() + interval '24 hours')",
                        new MapSqlParameterSource()
                                .addValue("sess", sessionUuid)
                                .addValue("wfId", workflowId)
                                .addValue("convId", conversationId)
                                .addValue("maxRetries", 3)
                                .addValue("maxInpaints", 2));

                state = Map.of(
                        "workflow_id", workflowId,
                        "session_uuid", sessionUuid,
                        "workflow_path", workflowPath.toString());
                conversationWorkflows.put(conversationId, state);
            }

            String imageUuid = UUID.randomUUID().toString();
            ObjectNode genMsg = mapper.createObjectNode();
            genMsg.put("image_uuid", imageUuid);
            genMsg.put("session_uuid", state.get("session_uuid"));
            genMsg.put("sequence_number", 0);
            genMsg.put("workflow_path", state.get("workflow_path"));
            genMsg.put("workflow_id", state.get("workflow_id"));
            genMsg.put("conversation_id", conversationId);
            genMsg.put("prompt", prompt);
            genMsg.put("model_type", modelType);
            genMsg.set("workflow_params", params);
            genMsg.set("graph_ops", graphOps);
            genMsg.set("loras", loras);

            jmsTemplate.convertAndSend("loop.generate", mapper.writeValueAsString(genMsg));
            log.info("Generation queued: " + imageUuid + " for conversation " + conversationId);
            return state;
        } catch (Exception e) {
            log.warning("queueNewGeneration failed: " + e.getMessage());
            return null;
        }
    }

    private String createWorkflow(String conversationId, String name, String workflowPath,
                                   int maxRetries, int maxInpaints) {
        try {
            List<String> ids = jdbc.queryForList(
                    "INSERT INTO workflows (conversation_id, name, workflow_path, max_retries, max_inpaints) " +
                    "VALUES (:convId::uuid, :name, :path, :maxRetries, :maxInpaints) " +
                    "RETURNING workflow_id::text",
                    new MapSqlParameterSource()
                            .addValue("convId", conversationId)
                            .addValue("name", name)
                            .addValue("path", workflowPath)
                            .addValue("maxRetries", maxRetries)
                            .addValue("maxInpaints", maxInpaints),
                    String.class);
            return ids.isEmpty() ? null : ids.get(0);
        } catch (Exception e) {
            log.warning("Failed to create workflow: " + e.getMessage());
            return null;
        }
    }

    // ── VLM eval prompt persistence ────────────────────────────────────────────

    private void handleVlmEvalPrompt(String conversationId, JsonNode decision) {
        if (conversationId == null) return;
        String evalPrompt = nullIfBlank(decision.path("vlm_eval_prompt").asText(""));
        if (evalPrompt == null) return;
        try {
            jdbc.update(
                    "INSERT INTO session_vlm_prompts (conversation_id, eval_prompt) " +
                    "VALUES (:convId::uuid, :prompt)",
                    Map.of("convId", conversationId, "prompt", evalPrompt));
        } catch (Exception e) {
            log.warning("Failed to save vlm_eval_prompt: " + e.getMessage());
        }
    }

    // ── Chat message persistence ───────────────────────────────────────────────

    void saveChatMessage(String conversationId, String workflowId, String role, String content) {
        if (conversationId == null || content == null || content.isBlank()) return;
        try {
            if (workflowId != null) {
                jdbc.update(
                        "INSERT INTO chat_messages (conversation_id, workflow_id, role, content) " +
                        "VALUES (:convId::uuid, :wfId::uuid, :role, :content)",
                        new MapSqlParameterSource()
                                .addValue("convId", conversationId)
                                .addValue("wfId", workflowId)
                                .addValue("role", role)
                                .addValue("content", content));
            } else {
                jdbc.update(
                        "INSERT INTO chat_messages (conversation_id, role, content) " +
                        "VALUES (:convId::uuid, :role, :content)",
                        Map.of("convId", conversationId, "role", role, "content", content));
            }
        } catch (Exception e) {
            log.warning("Failed to save chat message: " + e.getMessage());
        }
    }

    // ── Scoring context for conversation ───────────────────────────────────────

    private String buildScoringMessage(String imageUuid, JsonNode msg, Budget budget) {
        JsonNode scores       = msg.path("scores");
        double clipScore      = scores.path("clip_score").asDouble(0.0);
        double artifactScore  = scores.path("artifact_score").asDouble(1.0);
        JsonNode vlmScores    = scores.path("vlm_scores");
        String prompt         = msg.path("prompt").asText("");
        String verdictType    = msg.path("verdict").asText("candidate");
        String rejectionReason = nullIfBlank(msg.path("rejection_reason").asText(""));
        String imagePath      = nullIfBlank(msg.path("image_path").asText(""));
        Double northStarSim   = scores.has("north_star_similarity")
                ? scores.path("north_star_similarity").asDouble() : null;
        JsonNode vlmIssues    = msg.path("vlm_issues");
        JsonNode vlmRecs      = msg.path("vlm_recs");

        String imageUrl = imagePath != null ? "file://" + imagePath : "(unknown)";

        StringBuilder sb = new StringBuilder();
        sb.append("[SCORING RESULT]\n");
        sb.append("image_uuid: ").append(imageUuid).append("\n");
        sb.append("image_url:  ").append(imageUrl).append("\n");
        sb.append("verdict:    ").append(verdictType).append("\n");
        if (rejectionReason != null) sb.append("rejected:   ").append(rejectionReason).append("\n");
        sb.append("\n");
        sb.append(String.format("CLIP score:       %.4f%n", clipScore));
        sb.append(String.format("Artifact conf:    %.4f%n", artifactScore));
        if (northStarSim != null) sb.append(String.format("North star sim:   %.4f%n", northStarSim));
        sb.append("\nVLM scores:\n");
        for (String dim : new String[]{"photorealism","anatomical_coherence","interaction_plausibility",
                "lighting_consistency","prompt_adherence"}) {
            sb.append(String.format("  %-30s %s%n", dim, vlmScores.has(dim) ? vlmScores.get(dim).asText() : "n/a"));
        }
        if (vlmIssues.isArray() && vlmIssues.size() > 0) {
            sb.append("\nIssues:\n");
            for (JsonNode issue : vlmIssues) sb.append("  - ").append(issue.asText()).append("\n");
        }
        if (vlmRecs.isArray() && vlmRecs.size() > 0) {
            sb.append("\nRecommendations:\n");
            for (JsonNode rec : vlmRecs) sb.append("  - ").append(rec.asText()).append("\n");
        }
        sb.append("\nGeneration prompt: ").append(prompt).append("\n");
        sb.append(String.format("Budget remaining: %d retries, %d inpaints%n",
                budget.maxRetries() - budget.retriesUsed(),
                budget.maxInpaints() - budget.inpaintsUsed()));
        return sb.toString();
    }

    // ── Budget ─────────────────────────────────────────────────────────────────

    private record Budget(int retriesUsed, int inpaintsUsed, int maxRetries, int maxInpaints) {}

    private Budget loadBudget(String sessionUuid) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT retries_used, inpaints_used, max_retries, max_inpaints " +
                "FROM budget WHERE session_uuid = :sess::uuid",
                Map.of("sess", sessionUuid));
        if (rows.isEmpty()) {
            return new Budget(0, 0, cfg.getDecisions().getMaxRetries(), cfg.getDecisions().getMaxInpaints());
        }
        Map<String, Object> r = rows.get(0);
        return new Budget(
                ((Number) r.get("retries_used")).intValue(),
                ((Number) r.get("inpaints_used")).intValue(),
                ((Number) r.get("max_retries")).intValue(),
                ((Number) r.get("max_inpaints")).intValue());
    }

    private void incrementBudget(String sessionUuid, String field) {
        jdbc.update(
                "UPDATE budget SET " + field + " = " + field + " + 1 " +
                "WHERE session_uuid = :sess::uuid",
                Map.of("sess", sessionUuid));
    }

    // ── Agentic loop ───────────────────────────────────────────────────────────

    private JsonNode runAgenticLoop(List<ObjectNode> messages, boolean thinkingEnabled) {
        int maxTokens = thinkingEnabled ? cfg.getMaxTokensThinking() : cfg.getMaxTokens();
        ArrayNode tools = buildToolDefinitions();

        List<ObjectNode> conversation = new ArrayList<>(messages);

        for (int i = 0; i < cfg.getMaxToolIterations(); i++) {
            ObjectNode body = mapper.createObjectNode();
            body.put("model", resolvedModelId);
            body.put("temperature", cfg.getTemperature());
            body.put("max_tokens", maxTokens);
            body.set("tools", tools);
            ArrayNode msgArray = body.putArray("messages");
            for (ObjectNode m : conversation) msgArray.add(m);

            ObjectNode response;
            try {
                byte[] bodyBytes = mapper.writeValueAsBytes(body);
                response = llmClient.post()
                        .uri("/chat/completions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(bodyBytes)
                        .retrieve()
                        .body(ObjectNode.class);
            } catch (Exception e) {
                log.warning("LLM inference failed (iteration " + i + "): " + e.getMessage());
                return null;
            }

            JsonNode choice  = response.path("choices").path(0);
            JsonNode message = choice.path("message");
            String finish    = choice.path("finish_reason").asText("");

            conversation.add((ObjectNode) message.deepCopy());

            if ("tool_calls".equals(finish) && message.has("tool_calls")) {
                for (JsonNode toolCall : message.get("tool_calls")) {
                    String toolCallId = toolCall.path("id").asText();
                    String toolName   = toolCall.path("function").path("name").asText();
                    String argsJson   = toolCall.path("function").path("arguments").asText("{}");
                    String result     = executeToolCall(toolName, argsJson, conversation);

                    ObjectNode toolResult = mapper.createObjectNode();
                    toolResult.put("role", "tool");
                    toolResult.put("tool_call_id", toolCallId);
                    toolResult.put("content", result);
                    conversation.add(toolResult);
                }
                continue;
            }

            String raw = message.path("content").asText("").strip();
            JsonNode parsed = parseDecisionJson(raw);
            if (parsed != null) return parsed;

            // Model returned non-JSON (conversational text or truncated response).
            // Wrap it so the user sees the content rather than a hard error.
            if (!raw.isEmpty()) {
                log.warning("LLM returned non-JSON content, wrapping as await_input: " + raw.substring(0, Math.min(80, raw.length())));
                return mapper.createObjectNode()
                        .put("decision", "await_input")
                        .put("message", raw)
                        .put("confidence", 0.5);
            }
            return null;
        }

        log.warning("Tool iteration cap reached without final decision");
        return null;
    }

    // ── Tool definitions ───────────────────────────────────────────────────────

    private ArrayNode buildToolDefinitions() {
        ArrayNode tools = mapper.createArrayNode();
        tools.add(buildTool("analyze_image",
                "Ask the VLM a free-form question about the current candidate image. " +
                "Use image_path from your context as image_url. Ask targeted questions about " +
                "visual quality, alignment with the north star, or specific defects.",
                Map.of(
                    "image_url", "file:// URI of the image (image_url from scoring context)",
                    "question", "Your specific question about the image"),
                List.of("image_url", "question")));

        tools.add(buildTool("get_available_models",
                "Query ComfyUI for all installed models: checkpoints, LoRAs, ControlNets, VAEs, " +
                "and UNET models (Flux), plus available workflow files. " +
                "Call this when you need to know what is installed — for initial workflow setup, " +
                "before proposing LoRA or ControlNet use, or to confirm a download completed.",
                Map.of(), List.of()));

        tools.add(buildTool("get_workflow",
                "Read the JSON graph of a workflow file by name. " +
                "Call this to inspect the current workflow topology before proposing graph changes.",
                Map.of("name", "Workflow filename, e.g. sdxl_base.json"),
                List.of("name")));

        tools.add(buildTool("install_model",
                "Download a model from HuggingFace into ComfyUI's model directories. " +
                "ALWAYS use await_input to propose the model and get user approval before calling this. " +
                "The download runs in the background — return a message telling the user it has started " +
                "and to let you know when they want to check if it is ready. " +
                "Once complete, get_available_models will show it.",
                Map.of(
                    "repo_id",    "HuggingFace repo ID, e.g. Lykon/absolute-reality-v1-8-1",
                    "filename",   "Specific file to download, e.g. absolutereality_v181.safetensors",
                    "model_type", "One of: checkpoint, lora, vae, unet, clip, t5"),
                List.of("repo_id", "filename", "model_type")));

        tools.add(buildTool("get_system_prompt",
                "Read your current system prompt from disk. Call this after update_system_prompt " +
                "to verify what you wrote, or any time you want to inspect or quote your own " +
                "instructions. Returns the live file contents.",
                Map.of(), List.of()));

        tools.add(buildTool("update_system_prompt",
                "Rewrite your own system prompt. Use this when you identify an error, " +
                "gap, or improvement in your operating instructions — a tool description that " +
                "is wrong, a decision rule that led to a bad outcome, a format requirement " +
                "that is missing, or a calibration you want to persist. Write the COMPLETE new " +
                "system prompt, not just the changed section. The new prompt takes effect " +
                "immediately: subsequent LLM calls in this session and all future invocations " +
                "will use it. Call get_system_prompt afterward to verify.",
                Map.of("content", "The complete new system prompt text"),
                List.of("content")));

        return tools;
    }

    private ObjectNode buildTool(String name, String description,
                                  Map<String, String> propDescriptions,
                                  List<String> required) {
        ObjectNode tool = mapper.createObjectNode();
        tool.put("type", "function");
        ObjectNode fn = mapper.createObjectNode();
        fn.put("name", name);
        fn.put("description", description);
        ObjectNode params = mapper.createObjectNode();
        params.put("type", "object");
        ObjectNode props = mapper.createObjectNode();
        propDescriptions.forEach((k, v) -> {
            ObjectNode p = mapper.createObjectNode();
            p.put("type", "string");
            p.put("description", v);
            props.set(k, p);
        });
        params.set("properties", props);
        ArrayNode req = mapper.createArrayNode();
        required.forEach(req::add);
        params.set("required", req);
        fn.set("parameters", params);
        tool.set("function", fn);
        return tool;
    }

    // ── Tool execution ─────────────────────────────────────────────────────────

    private String executeToolCall(String toolName, String argsJson, List<ObjectNode> conversation) {
        return switch (toolName) {
            case "analyze_image" -> executeAnalyzeImage(argsJson);
            case "get_available_models" -> executeGetAvailableModels();
            case "get_workflow" -> executeGetWorkflow(argsJson);
            case "install_model" -> executeInstallModel(argsJson);
            case "get_system_prompt" -> executeGetSystemPrompt();
            case "update_system_prompt" -> executeUpdateSystemPrompt(argsJson, conversation);
            default -> "Unknown tool: " + toolName;
        };
    }

    private String executeAnalyzeImage(String argsJson) {
        try {
            JsonNode args = mapper.readTree(argsJson);
            ObjectNode reqBody = mapper.createObjectNode();
            reqBody.put("image_url", args.path("image_url").asText(""));
            reqBody.put("question", args.path("question").asText(""));
            JsonNode result = vlmClient.post().uri("/analyze")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(reqBody)
                    .retrieve()
                    .body(JsonNode.class);
            return result.path("answer").asText("(no answer)");
        } catch (Exception e) {
            log.warning("analyze_image failed: " + e.getMessage());
            return "Error: " + e.getMessage();
        }
    }

    private String executeGetAvailableModels() {
        try {
            StringBuilder sb = new StringBuilder();

            appendModelList(sb, "Checkpoints",   "/object_info/CheckpointLoaderSimple", "CheckpointLoaderSimple", "ckpt_name",
                    "  (none — install a checkpoint via install_model)\n");
            appendModelList(sb, "LoRAs",          "/object_info/LoraLoader",             "LoraLoader",             "lora_name",
                    "  (none installed)\n");
            appendModelList(sb, "ControlNets",    "/object_info/ControlNetLoader",        "ControlNetLoader",       "control_net_name",
                    "  (none installed)\n");
            appendModelList(sb, "VAEs",           "/object_info/VAELoader",               "VAELoader",              "vae_name",
                    "  (none installed)\n");
            appendModelList(sb, "UNETs (Flux)",   "/object_info/UNETLoader",              "UNETLoader",             "unet_name",
                    null);

            List<String> wfNames = listWorkflowNames();
            sb.append("Available workflows: ").append(
                    wfNames.isEmpty() ? "(none)" : String.join(", ", wfNames)).append("\n");

            return sb.toString();
        } catch (Exception e) {
            return "Error querying models: " + e.getMessage();
        }
    }

    private void appendModelList(StringBuilder sb, String label, String uri,
                                  String nodeClass, String inputField, String emptyMsg) {
        try {
            JsonNode info = comfyuiClient.get().uri(uri).retrieve().body(JsonNode.class);
            JsonNode list = info.path(nodeClass).path("input").path("required").path(inputField).path(0);
            if (list.isArray() && list.size() > 0) {
                sb.append(label).append(":\n");
                for (JsonNode item : list) sb.append("  ").append(item.asText()).append("\n");
            } else if (emptyMsg != null) {
                sb.append(label).append(":\n").append(emptyMsg);
            }
        } catch (Exception e) {
            if (emptyMsg != null) {
                sb.append(label).append(": (ComfyUI unreachable — ").append(e.getMessage()).append(")\n");
            }
        }
    }

    private String executeInstallModel(String argsJson) {
        try {
            JsonNode args     = mapper.readTree(argsJson);
            String repoId     = args.path("repo_id").asText("").strip();
            String filename   = args.path("filename").asText("").strip();
            String modelType  = args.path("model_type").asText("checkpoint").strip();

            if (repoId.isEmpty() || filename.isEmpty()) {
                return "Error: repo_id and filename are required";
            }

            String subdir = switch (modelType) {
                case "lora"       -> "loras";
                case "vae"        -> "vae";
                case "unet"       -> "unet";
                case "clip"       -> "clip";
                case "t5"         -> "t5";
                default           -> "checkpoints";
            };

            Path destDir = comfyuiModelsDir.resolve(subdir);
            Files.createDirectories(destDir);

            // hf download <repo_id> <filename> --local-dir <dest>
            ProcessBuilder pb = new ProcessBuilder(
                    "hf", "download", repoId, filename, "--local-dir", destDir.toString())
                    .redirectOutput(new java.io.File("/tmp/ai-loop/model-download.log"))
                    .redirectError(new java.io.File("/tmp/ai-loop/model-download.log"));
            Process proc = pb.start();

            log.info("Model download started: " + repoId + "/" + filename + " → " + destDir);
            return "Download started: " + filename + " from " + repoId +
                   " → " + destDir + "\nPID: " + proc.pid() +
                   "\nLog: /tmp/ai-loop/model-download.log\n" +
                   "Call get_available_models() after the user confirms it is ready.";
        } catch (Exception e) {
            log.warning("install_model failed: " + e.getMessage());
            return "Error starting download: " + e.getMessage();
        }
    }

    private String executeGetSystemPrompt() {
        return readSystemPrompt();
    }

    private String executeUpdateSystemPrompt(String argsJson, List<ObjectNode> conversation) {
        try {
            JsonNode args = mapper.readTree(argsJson);
            String content = args.path("content").asText("").strip();
            if (content.isEmpty()) return "Error: content is required";
            Files.writeString(systemPromptPath, content);
            log.info("System prompt updated by tactical LLM (" + content.length() + " chars)");

            // Apply to the active conversation immediately so subsequent LLM calls in
            // this session use the new prompt. Index 0 is always the system message.
            if (!conversation.isEmpty()) {
                String contextBlock = conversation.get(0).path("content").asText("");
                int sep = contextBlock.indexOf("\n\n═══ CURRENT CONTEXT");
                String newSystemContent = sep >= 0
                        ? content + contextBlock.substring(sep)
                        : content;
                conversation.get(0).put("content", newSystemContent);
            }

            return "System prompt updated (" + content.length() + " chars) and applied to " +
                   "the current session. Call get_system_prompt to verify.";
        } catch (Exception e) {
            log.warning("update_system_prompt failed: " + e.getMessage());
            return "Error: " + e.getMessage();
        }
    }

    private String executeGetWorkflow(String argsJson) {
        try {
            JsonNode args = mapper.readTree(argsJson);
            String name = args.path("name").asText("").strip();
            if (name.isEmpty()) return "Error: name is required";

            Path wfPath = workflowsDir.resolve(name);
            if (!Files.exists(wfPath)) {
                List<String> available = listWorkflowNames();
                return "Workflow '" + name + "' not found. Available: " + String.join(", ", available);
            }

            // Return node summary (not full JSON — too large)
            String json = Files.readString(wfPath);
            JsonNode wf = mapper.readTree(json);
            StringBuilder sb = new StringBuilder("Workflow: " + name + "\nNodes:\n");
            List<String> nodeIds = new ArrayList<>();
            wf.fieldNames().forEachRemaining(nodeIds::add);
            nodeIds.sort((a, b) -> {
                try { return Integer.compare(Integer.parseInt(a), Integer.parseInt(b)); }
                catch (NumberFormatException e) { return a.compareTo(b); }
            });
            for (String nodeId : nodeIds) {
                JsonNode node = wf.get(nodeId);
                String ct    = node.path("class_type").asText("?");
                String title = node.path("_meta").path("title").asText("");
                sb.append(String.format("  \"%s\"  %s%s%n", nodeId, ct,
                        title.isBlank() ? "" : "  [" + title + "]"));
                // Show key inputs
                JsonNode inputs = node.path("inputs");
                inputs.fields().forEachRemaining(e -> {
                    JsonNode val = e.getValue();
                    if (!val.isArray()) {
                        sb.append(String.format("         %s = %s%n", e.getKey(), val.asText()));
                    }
                });
            }
            return sb.toString();
        } catch (Exception e) {
            log.warning("get_workflow failed: " + e.getMessage());
            return "Error: " + e.getMessage();
        }
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private List<String> listWorkflowNames() {
        try {
            if (!Files.isDirectory(workflowsDir)) return List.of();
            try (var stream = Files.list(workflowsDir)) {
                return stream
                        .filter(p -> p.getFileName().toString().endsWith(".json"))
                        .map(p -> p.getFileName().toString())
                        .sorted()
                        .collect(Collectors.toList());
            }
        } catch (Exception e) {
            return List.of();
        }
    }

    private JsonNode parseDecisionJson(String raw) {
        try {
            if (raw.contains("<think>")) {
                int end = raw.indexOf("</think>");
                if (end == -1) return null;
                raw = raw.substring(end + "</think>".length()).strip();
            }
            if (raw.startsWith("```")) {
                String[] parts = raw.split("```");
                raw = parts.length > 1 ? parts[1] : raw;
                if (raw.startsWith("json")) raw = raw.substring(4);
                raw = raw.strip();
            }
            return mapper.readTree(raw);
        } catch (Exception e) {
            log.warning("Failed to parse decision JSON: " + e.getMessage());
            return null;
        }
    }

    private void initiateShutdown() {
        log.info("Escalation: initiating graceful pipeline shutdown");
        new Thread(() -> {
            try { Thread.sleep(500); } catch (InterruptedException ignored) {}
            SpringApplication.exit(applicationContext, () -> 0);
        }).start();
    }

    private JsonNode heuristicDecision(JsonNode msg, Budget budget) {
        JsonNode scores   = msg.path("scores");
        double clipScore  = scores.path("clip_score").asDouble(0.0);
        JsonNode vlmScores = scores.path("vlm_scores");
        double vlmMean    = computeVlmMean(vlmScores);

        if (vlmMean >= cfg.getDecisions().getAcceptVlmMeanMin()
                && clipScore >= cfg.getDecisions().getAcceptClipMin()) {
            return mapper.createObjectNode()
                    .put("decision", "accept")
                    .put("message", "Image accepted — quality thresholds met.")
                    .put("reasoning", "Heuristic accept.")
                    .put("confidence", 0.7);
        }
        if (budget.retriesUsed() < budget.maxRetries()) {
            return mapper.createObjectNode()
                    .put("decision", "retry")
                    .put("message", String.format("Retrying — scores below threshold (vlm_mean=%.1f, clip=%.3f).", vlmMean, clipScore))
                    .put("reasoning", "Heuristic retry.")
                    .put("confidence", 0.5)
                    .put("retry_prompt", msg.path("prompt").asText(""));
        }
        return mapper.createObjectNode()
                .put("decision", "give_up")
                .put("message", "Budget exhausted — could not produce a satisfactory image.")
                .put("reasoning", "Budget exhausted (heuristic).")
                .put("confidence", 1.0);
    }

    private double computeVlmMean(JsonNode vlmScores) {
        String[] dims = {"photorealism", "anatomical_coherence", "interaction_plausibility",
                         "lighting_consistency", "prompt_adherence"};
        double sum = 0;
        int count  = 0;
        for (String d : dims) {
            if (vlmScores.has(d)) { sum += vlmScores.get(d).asDouble(); count++; }
        }
        return count > 0 ? sum / count : 0.0;
    }

    private boolean shouldThink(JsonNode msg, Budget budget) {
        if (budget.retriesUsed() >= budget.maxRetries() && budget.inpaintsUsed() >= budget.maxInpaints()) return false;
        JsonNode vlmScores = msg.path("scores").path("vlm_scores");
        double clipScore   = msg.path("scores").path("clip_score").asDouble(0.0);
        double vlmMean     = computeVlmMean(vlmScores);
        return vlmMean >= 4.5 && vlmMean < 7.5 && clipScore >= 0.20;
    }

    // ── Model ID resolution ────────────────────────────────────────────────────

    private static String resolveModelId(String configured, RestClient llmClient, ObjectMapper mapper) {
        if (configured != null && !configured.isBlank()
                && !"default".equals(configured) && !"auto".equals(configured)) {
            return configured;
        }
        try {
            JsonNode resp = llmClient.get().uri("/models")
                    .header("Authorization", "Bearer none")
                    .retrieve().body(JsonNode.class);
            String id = resp.path("data").path(0).path("id").asText("");
            if (!id.isBlank()) {
                Logger.getLogger(TacticalLlmCaller.class.getName())
                        .info("Resolved tactical LLM model ID: " + id);
                return id;
            }
        } catch (Exception e) {
            Logger.getLogger(TacticalLlmCaller.class.getName())
                    .warning("Could not resolve tactical LLM model ID: " + e.getMessage());
        }
        return configured != null ? configured : "default";
    }

    // ── System prompt ─────────────────────────────────────────────────────────

    private String readSystemPrompt() {
        try {
            return Files.readString(systemPromptPath);
        } catch (Exception e) {
            log.warning("Failed to read system prompt from " + systemPromptPath + ": " + e.getMessage());
            return "You are a tactical image generation agent. Output JSON decisions only.";
        }
    }

    // ── WebSocket send helpers ─────────────────────────────────────────────────

    private void sendJson(WebSocketSession session, ObjectNode node) throws IOException {
        if (session == null || !session.isOpen()) return;
        synchronized (session) {
            session.sendMessage(new TextMessage(mapper.writeValueAsString(node)));
        }
    }

    private ObjectNode jsonChunk(String text) {
        ObjectNode n = mapper.createObjectNode();
        n.put("type", "chunk");
        n.put("text", text);
        return n;
    }

    private ObjectNode jsonDone() {
        ObjectNode n = mapper.createObjectNode();
        n.put("type", "done");
        return n;
    }

    private ObjectNode jsonError(String text) {
        ObjectNode n = mapper.createObjectNode();
        n.put("type", "error");
        n.put("text", text);
        return n;
    }

    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }
}
