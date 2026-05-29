package ai.image.pipeline.worker;

import ai.image.pipeline.config.TacticalLlmConfig;
import ai.image.pipeline.service.ContextService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.client.RestClient;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.logging.Logger;

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
    private final String systemPrompt;

    public TacticalLlmCaller(
            PlatformTransactionManager txManager,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            TacticalLlmConfig cfg,
            ContextService contextService,
            @Qualifier("tacticalLlmClient") RestClient llmClient) {
        this.requiresNew = new TransactionTemplate(txManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
        this.cfg = cfg;
        this.contextService = contextService;
        this.llmClient = llmClient;
        this.systemPrompt = loadSystemPrompt();
    }

    // ── JMS listener ───────────────────────────────────────────────────────────

    @JmsListener(destination = "loop.verdicts")
    public void onVerdict(String body) throws Exception {
        JsonNode msg = mapper.readTree(body);
        String imageUuid = msg.get("image_uuid").asText();

        // Skip if already decided (redelivery after Tx2 committed but before ACK)
        boolean alreadyDecided = !jdbc.queryForList(
                "SELECT 1 FROM images WHERE image_uuid = :id::uuid AND decision IS NOT NULL",
                Map.of("id", imageUuid)).isEmpty();
        if (alreadyDecided) {
            log.info("Skipping already-decided verdict for " + imageUuid);
            return;
        }

        // Tx1 (REQUIRES_NEW): stage
        final String payloadJson = body;
        requiresNew.execute(status -> {
            jdbc.update(
                    "INSERT INTO pending_decisions (image_uuid, session_uuid, verdict_payload) " +
                    "VALUES (:id::uuid, :sess::uuid, :payload::jsonb) ON CONFLICT DO NOTHING",
                    Map.of(
                            "id", imageUuid,
                            "sess", msg.get("session_uuid").asText(),
                            "payload", payloadJson));
            return null;
        });

        processVerdict(imageUuid, msg);
    }

    // ── Crash recovery ─────────────────────────────────────────────────────────

    @EventListener(ApplicationReadyEvent.class)
    public void recoverPending() {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT image_uuid::text, verdict_payload::text, decision " +
                "FROM pending_decisions",
                Map.of());
        for (Map<String, Object> row : rows) {
            String imageUuid = (String) row.get("image_uuid");
            String decision = (String) row.get("decision");
            String payloadJson = (String) row.get("verdict_payload");
            log.info("Recovery: resuming pending decision for " + imageUuid);
            try {
                JsonNode msg = mapper.readTree(payloadJson);
                if (decision != null) {
                    // LLM already ran but Tx2 failed — re-execute Tx2
                    completeTx2(imageUuid, msg, decision, null);
                } else {
                    processVerdict(imageUuid, msg);
                }
            } catch (Exception e) {
                log.warning("Recovery failed for " + imageUuid + ": " + e.getMessage());
            }
        }
    }

    // ── Core processing ────────────────────────────────────────────────────────

    private void processVerdict(String imageUuid, JsonNode msg) throws Exception {
        String sessionUuid = msg.path("session_uuid").asText();
        String workflowId = nullIfBlank(msg.path("workflow_id").asText(""));
        String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));

        Budget budget = loadBudget(sessionUuid);

        // Check cancellation
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

        String decisionPrompt = buildDecisionPrompt(imageUuid, msg, budget, workflowId, conversationId);
        JsonNode decision = runInference(decisionPrompt);

        if (decision == null || !decision.has("decision")) {
            decision = heuristicDecision(msg, budget);
        }

        String action = decision.path("decision").asText("give_up");

        // Record decision in pending_decisions before executing (crash recovery if Tx2 fails)
        final String finalAction = action;
        requiresNew.execute(status -> {
            jdbc.update(
                    "UPDATE pending_decisions SET decision = :decision WHERE image_uuid = :id::uuid",
                    Map.of("decision", finalAction, "id", imageUuid));
            return null;
        });

        completeTx2(imageUuid, msg, action, decision);
    }

    private void completeTx2(String imageUuid, JsonNode msg, String action, JsonNode decision) {
        requiresNew.execute(status -> {
            try {
                jdbc.update("DELETE FROM pending_decisions WHERE image_uuid = :id::uuid",
                        Map.of("id", imageUuid));

                String sessionUuid = msg.path("session_uuid").asText();
                String workflowId = nullIfBlank(msg.path("workflow_id").asText(""));
                String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));

                jdbc.update(
                        "UPDATE images SET decision = :decision WHERE image_uuid = :id::uuid",
                        Map.of("decision", action, "id", imageUuid));

                switch (action) {
                    case "accept" -> log.info("Accepted " + imageUuid);

                    case "retry", "inpaint" -> {
                        // inpaint not yet implemented — downgrade to retry
                        String effectiveAction = "retry";
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

                        String newImageUuid = UUID.randomUUID().toString();
                        int newSeq = msg.path("sequence_number").asInt(0) + 1;
                        String retryPrompt = decision != null ? decision.path("retry_prompt").asText(msg.path("prompt").asText("")) : msg.path("prompt").asText("");
                        JsonNode retryParams = decision != null && decision.has("retry_params") ? decision.get("retry_params") : mapper.createObjectNode();
                        JsonNode retryGraphOps = decision != null && decision.has("retry_graph_ops") ? decision.get("retry_graph_ops") : mapper.createArrayNode();
                        JsonNode retryLoras = decision != null && decision.has("retry_loras") ? decision.get("retry_loras") : mapper.createArrayNode();
                        String retryModel = decision != null ? nullIfBlank(decision.path("retry_model").asText("")) : null;

                        // Merge original workflow_params with retry_params
                        ObjectNode baseParams = (ObjectNode) msg.path("scores").path("workflow_params");
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
                }
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
            return null;
        });
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

    // ── LLM inference ──────────────────────────────────────────────────────────

    private JsonNode runInference(String decisionPrompt) {
        boolean thinking = decisionPrompt.endsWith("/think");
        int maxTokens = thinking ? cfg.getMaxTokensThinking() : cfg.getMaxTokens();

        try {
            ObjectNode body = mapper.createObjectNode();
            body.put("model", cfg.getModel());
            body.put("temperature", cfg.getTemperature());
            body.put("max_tokens", maxTokens);
            ArrayNode messages = body.putArray("messages");
            messages.add(mapper.createObjectNode().put("role", "system").put("content", systemPrompt));
            messages.add(mapper.createObjectNode().put("role", "user").put("content", decisionPrompt));

            ObjectNode response = llmClient.post()
                    .uri("/chat/completions")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve()
                    .body(ObjectNode.class);

            String raw = response.path("choices").path(0).path("message").path("content").asText("").strip();

            // Strip Qwen3 chain-of-thought block
            if (raw.contains("<think>")) {
                int end = raw.indexOf("</think>");
                if (end == -1) return null;
                raw = raw.substring(end + "</think>".length()).strip();
            }

            // Strip accidental markdown fences
            if (raw.startsWith("```")) {
                String[] parts = raw.split("```");
                raw = parts.length > 1 ? parts[1] : raw;
                if (raw.startsWith("json")) raw = raw.substring(4);
                raw = raw.strip();
            }

            return mapper.readTree(raw);
        } catch (Exception e) {
            log.warning("LLM inference failed: " + e.getMessage());
            return null;
        }
    }

    private JsonNode heuristicDecision(JsonNode msg, Budget budget) {
        JsonNode scores = msg.path("scores");
        double clipScore = scores.path("clip_score").asDouble(0.0);
        JsonNode vlmScores = scores.path("vlm_scores");
        double vlmMean = computeVlmMean(vlmScores);

        if (vlmMean >= cfg.getDecisions().getAcceptVlmMeanMin()
                && clipScore >= cfg.getDecisions().getAcceptClipMin()) {
            return mapper.createObjectNode()
                    .put("decision", "accept").put("reasoning", "Heuristic accept.").put("confidence", 0.7);
        }
        if (budget.retriesUsed() < budget.maxRetries()) {
            return mapper.createObjectNode()
                    .put("decision", "retry")
                    .put("reasoning", String.format("Heuristic retry (vlm_mean=%.1f, clip=%.3f).", vlmMean, clipScore))
                    .put("confidence", 0.5)
                    .put("retry_prompt", msg.path("prompt").asText(""));
        }
        return mapper.createObjectNode()
                .put("decision", "give_up").put("reasoning", "Budget exhausted (heuristic).").put("confidence", 1.0);
    }

    private double computeVlmMean(JsonNode vlmScores) {
        String[] dims = {"photorealism", "anatomical_coherence", "interaction_plausibility",
                         "lighting_consistency", "prompt_adherence"};
        double sum = 0;
        int count = 0;
        for (String d : dims) {
            if (vlmScores.has(d)) { sum += vlmScores.get(d).asDouble(); count++; }
        }
        return count > 0 ? sum / count : 0.0;
    }

    // ── Decision prompt construction ───────────────────────────────────────────

    private String buildDecisionPrompt(String imageUuid, JsonNode msg, Budget budget,
                                        String workflowId, String conversationId) {
        JsonNode scores = msg.path("scores");
        double clipScore = scores.path("clip_score").asDouble(0.0);
        double artifactScore = scores.path("artifact_score").asDouble(1.0);
        JsonNode vlmScores = scores.path("vlm_scores");
        String prompt = msg.path("prompt").asText("");
        String verdictType = msg.path("verdict").asText("candidate");
        String rejectionReason = nullIfBlank(msg.path("rejection_reason").asText(""));

        // Session history
        List<Map<String, Object>> history = jdbc.queryForList(
                """
                SELECT sequence_number, verdict, rejection_reason, clip_score,
                       artifact_score, vlm_scores::text, prompt, image_uuid::text
                FROM images WHERE session_uuid = :sess::uuid
                ORDER BY sequence_number
                """,
                Map.of("sess", msg.path("session_uuid").asText()));

        // Load workflow graph
        String workflowGraph = null;
        String workflowPath = msg.path("workflow_path").asText("");
        if (!workflowPath.isBlank()) {
            try {
                workflowGraph = Files.readString(Path.of(workflowPath));
            } catch (Exception ignored) {}
        }

        // Creative context from PostgreSQL
        String northStar = contextService.northStar();
        String sessionDirection = contextService.sessionDirection(workflowId);
        String tasteSynthesis = contextService.tasteSynthesis(workflowId);

        // VLM issues and recs (from verdict message — added by Scorer in Phase 4 update)
        JsonNode vlmIssues = msg.path("vlm_issues");
        JsonNode vlmRecs = msg.path("vlm_recs");

        boolean shouldThink = shouldThink(msg, budget);
        StringBuilder sb = new StringBuilder();

        sb.append("── IMAGE UNDER EVALUATION ───────────────────────────────────\n");
        sb.append("  image_uuid:        ").append(imageUuid).append("\n");
        sb.append("  verdict:           ").append(verdictType).append("\n");
        sb.append("  rejection_reason:  ").append(rejectionReason != null ? rejectionReason : "null").append("\n\n");

        sb.append("── SCORES ───────────────────────────────────────────────────\n");
        sb.append(String.format("  CLIP score:        %.4f%n", clipScore));
        sb.append(String.format("  Artifact conf:     %.4f%n%n", artifactScore));
        sb.append("  VLM dimensions:\n");
        for (String dim : new String[]{"photorealism","anatomical_coherence","interaction_plausibility","lighting_consistency","prompt_adherence"}) {
            sb.append(String.format("  %-28s %s%n", dim, vlmScores.has(dim) ? vlmScores.get(dim).asText() : "n/a"));
        }
        sb.append("\n  VLM issues:\n");
        if (vlmIssues.isArray() && vlmIssues.size() > 0) {
            for (JsonNode issue : vlmIssues) sb.append("    - ").append(issue.asText()).append("\n");
        } else { sb.append("    (none)\n"); }
        sb.append("\n  VLM recommendations:\n");
        if (vlmRecs.isArray() && vlmRecs.size() > 0) {
            for (JsonNode rec : vlmRecs) sb.append("    - ").append(rec.asText()).append("\n");
        } else { sb.append("    (none)\n"); }

        sb.append("\n── ORIGINAL PROMPT ──────────────────────────────────────────\n");
        sb.append("  ").append(prompt).append("\n\n");

        sb.append("── SESSION HISTORY (this session, ordered) ──────────────────\n");
        if (history.isEmpty()) {
            sb.append("  (none)\n");
        } else {
            for (Map<String, Object> rec : history) {
                double vlmMean = 0;
                try {
                    JsonNode vs = mapper.readTree((String) rec.get("vlm_scores"));
                    vlmMean = computeVlmMean(vs);
                } catch (Exception ignored) {}
                sb.append(String.format("  seq=%-3s verdict=%-9s clip=%.3f vlm_mean=%.1f%n",
                        rec.get("sequence_number"),
                        rec.get("verdict"),
                        rec.get("clip_score") != null ? ((Number)rec.get("clip_score")).doubleValue() : 0.0,
                        vlmMean));
                if (rec.get("rejection_reason") != null)
                    sb.append("       rejection_reason=").append(rec.get("rejection_reason")).append("\n");
                if (rec.get("prompt") != null) {
                    String p = (String) rec.get("prompt");
                    sb.append("       prompt: ").append(p.substring(0, Math.min(120, p.length()))).append("\n");
                }
            }
        }
        sb.append("\n");

        sb.append("── BUDGET ───────────────────────────────────────────────────\n");
        sb.append(String.format("  Retries remaining:  %d / %d%n", budget.maxRetries() - budget.retriesUsed(), budget.maxRetries()));
        sb.append(String.format("  Inpaints remaining: %d / %d%n%n", budget.maxInpaints() - budget.inpaintsUsed(), budget.maxInpaints()));

        if (northStar != null) {
            sb.append("── NORTH STAR (long-term artistic vision) ───────────────────\n");
            sb.append(northStar).append("\n\n");
        }
        if (sessionDirection != null) {
            sb.append("── SESSION DIRECTION (creative brief for this workflow) ─────\n");
            sb.append(sessionDirection).append("\n\n");
        }
        if (tasteSynthesis != null) {
            sb.append("── TASTE SYNTHESIS (learned user preferences) ───────────────\n");
            sb.append(tasteSynthesis).append("\n\n");
        }

        if (workflowGraph != null) {
            try {
                ObjectNode wf = (ObjectNode) mapper.readTree(workflowGraph);
                sb.append("── WORKFLOW GRAPH (current node topology) ───────────────────\n");
                List<String> nodeIds = new ArrayList<>();
                wf.fieldNames().forEachRemaining(nodeIds::add);
                nodeIds.sort((a, b) -> {
                    try { return Integer.compare(Integer.parseInt(a), Integer.parseInt(b)); }
                    catch (NumberFormatException e) { return a.compareTo(b); }
                });
                for (String nodeId : nodeIds) {
                    JsonNode node = wf.get(nodeId);
                    String ct = node.path("class_type").asText("?");
                    String title = node.path("_meta").path("title").asText("");
                    sb.append(String.format("  \"%s\"  %s%s%n", nodeId, ct, title.isBlank() ? "" : "  [" + title + "]"));
                }
                sb.append("\n");
            } catch (Exception ignored) {}
        }

        sb.append(shouldThink ? "/think" : "/no_think");
        return sb.toString();
    }

    private boolean shouldThink(JsonNode msg, Budget budget) {
        if (budget.retriesUsed() >= budget.maxRetries() && budget.inpaintsUsed() >= budget.maxInpaints()) return false;
        JsonNode vlmScores = msg.path("scores").path("vlm_scores");
        double clipScore = msg.path("scores").path("clip_score").asDouble(0.0);
        double vlmMean = computeVlmMean(vlmScores);
        return !(vlmMean >= 8.0 && clipScore >= 0.35);
    }

    // ── System prompt ──────────────────────────────────────────────────────────

    private String loadSystemPrompt() {
        String filePath = System.getProperty("tactical.system-prompt-file",
                "tactical-llm/system_prompt.md");
        try {
            return Files.readString(Path.of(filePath));
        } catch (Exception e) {
            log.warning("Could not load system prompt from " + filePath + ": " + e.getMessage());
            return DEFAULT_SYSTEM_PROMPT;
        }
    }

    private static final String DEFAULT_SYSTEM_PROMPT = """
You are the tactical executor in a multi-agent AI image generation pipeline.
Your job is to evaluate each generated image against the current creative vision
and decide the best next step: accept, retry, inpaint, or give_up.
On retry or inpaint, revise the prompt and optionally modify the ComfyUI workflow graph.
Your context (north star, direction, taste) is provided in the decision prompt below.

Respond with a single JSON object and nothing else:
{
  "decision": "accept" | "retry" | "inpaint" | "give_up",
  "reasoning": "<one or two sentences>",
  "confidence": <0.0-1.0>,
  "retry_prompt": "<comma-separated tags>",
  "retry_params": {},
  "retry_model": "",
  "retry_loras": [],
  "retry_graph_ops": []
}
""";

    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }
}
