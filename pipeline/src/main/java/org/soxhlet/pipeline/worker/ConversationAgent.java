package org.soxhlet.pipeline.worker;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.boot.SpringApplication;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.client.RestClient;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.logging.Logger;
import java.util.stream.Collectors;

public class ConversationAgent implements Runnable {

    private static final Logger log = Logger.getLogger(ConversationAgent.class.getName());
    private static final int SAFETY_LIMIT = 20;

    final String conversationId;
    private final ConversationAgentManager mgr;
    private final TransactionTemplate requiresNew;
    private final BlockingQueue<AgentEvent> queue = new LinkedBlockingQueue<>();
    private volatile boolean running = true;

    // Per-conversation workflow state (workflow_id, session_uuid, workflow_path)
    private Map<String, String> workflowState = null;

    // Download tracking: filename -> expected total bytes / destination directory
    private final Map<String, Long> downloadExpectedSizes = new ConcurrentHashMap<>();
    private final Map<String, Path> downloadDestDirs      = new ConcurrentHashMap<>();

    ConversationAgent(String conversationId, ConversationAgentManager mgr) {
        this.conversationId = conversationId;
        this.mgr = mgr;
        this.requiresNew = new TransactionTemplate(mgr.txManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    void enqueue(AgentEvent event) {
        queue.offer(event);
    }

    void stop() {
        running = false;
        queue.offer(new AgentEvent.UserMessage("__stop__")); // unblock take()
    }

    // -- Main loop --------------------------------------------------------------

    @Override
    public void run() {
        while (running) {
            try {
                AgentEvent event = queue.take();
                if (!running) break;
                if (event instanceof AgentEvent.UserMessage um && "__stop__".equals(um.text())) break;
                processEvent(event);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            } catch (Exception e) {
                log.warning("Agent " + conversationId + " error: " + e.getMessage());
            }
        }
        log.info("Agent stopped for conversation " + conversationId);
    }

    private void processEvent(AgentEvent event) {
        if (event instanceof AgentEvent.UserMessage um) {
            processUserMessage(um.text());
        } else if (event instanceof AgentEvent.Verdict v) {
            processVerdict(v.imageUuid(), v.payloadJson());
        } else if (event instanceof AgentEvent.GenerationFailed gf) {
            processGenerationFailed(gf.imageUuid(), gf.reason());
        }
    }

    // -- User message handling --------------------------------------------------

    private static boolean hasStyleKeyword(String text) {
        String t = text.toLowerCase();
        return t.contains("realistic") || t.contains("photorealistic") || t.contains("anime")
            || t.contains("manga") || t.contains("cartoon") || t.contains("digital art")
            || t.contains("oil paint") || t.contains("watercolor") || t.contains("sketch")
            || t.contains("3d render") || t.contains("painterly") || t.contains("cinematic")
            || t.contains("illustration") || t.contains("stylized") || t.contains("pixel art")
            || t.contains("dark fantasy") || t.contains("gothic") || t.contains("fantasy art")
            || t.contains("concept art") || t.contains("hyperrealistic");
    }

    private void processUserMessage(String text) {
        try {
            List<ObjectNode> messages = buildConversation(conversationId);

            // First turn + no explicit style -> append a mandatory reminder to the system message
            // so the model cannot proceed to tool calls before asking about style.
            // (MLX server requires exactly one system message at the start; we cannot insert a second.)
            if (messages.size() == 2 && !hasStyleKeyword(text)) {
                ObjectNode sysMsg = messages.get(0);
                String existing = sysMsg.path("content").asText();
                sysMsg.put("content", existing +
                        "\n\nMANDATORY FIRST-TURN REQUIREMENT: The user has not specified a visual style.\n" +
                        "You MUST respond with {\"decision\": \"await_input\"} asking a single focused question about style.\n" +
                        "Do NOT call get_available_models(). Do NOT call search_web(). Do NOT generate.\n" +
                        "Ask about style first. Nothing else is acceptable for this turn.");
            }

            JsonNode decision = runReasoningLoop(messages, false);

            if (decision == null) {
                mgr.chatBroadcast.sendError(conversationId, "The model did not respond. Please try again in a moment.");
                return;
            }

            String message = ConversationAgentManager.nullIfBlank(decision.path("message").asText(""));
            String action  = decision.path("decision").asText("await_input");

            handleVlmEvalPrompt(decision);

            if ("generate".equals(action)) {
                String ckptError = validateCheckpointGraphOps(decision);
                if (ckptError != null) {
                    log.warning("Checkpoint validation failed for conversation " + conversationId + ": " + ckptError);
                    messages.add(mgr.mapper.createObjectNode().put("role", "user").put("content", ckptError));
                    decision = runReasoningLoop(messages, false);
                    if (decision != null) {
                        message = ConversationAgentManager.nullIfBlank(decision.path("message").asText(""));
                        action  = decision.path("decision").asText("await_input");
                    }
                }
                if ("generate".equals(action)) {
                    Map<String, String> state = queueNewGeneration(decision);
                    if (state != null) {
                        if (message == null) message = "On it. Generation submitted.";
                        mgr.galleryBroadcast.decision("", "generate", "", 1.0,
                                state.get("workflow_id"), conversationId);
                    } else {
                        message = (message != null ? message + "\n\n" : "") +
                                  "Failed to queue generation. No workflow is available or an internal error occurred.";
                    }
                }
            }

            if (message != null) {
                saveChatMessage(null, "assistant", message);
                mgr.chatBroadcast.sendChunk(conversationId, message);
                mgr.chatBroadcast.sendDone(conversationId);
            } else {
                mgr.chatBroadcast.sendDone(conversationId);
            }

            maybeAutoTitle(messages);
            maybeExtractMemory();

        } catch (Exception e) {
            log.warning("processUserMessage error for " + conversationId + ": " + e.getMessage());
            mgr.chatBroadcast.sendError(conversationId, "Agent error: " + e.getMessage());
        }
    }

    // -- Verdict (scoring result) handling -------------------------------------

    private void processVerdict(String imageUuid, String payloadJson) {
        try {
            JsonNode msg          = mgr.mapper.readTree(payloadJson);
            String sessionUuid    = msg.path("session_uuid").asText();
            String workflowId     = ConversationAgentManager.nullIfBlank(msg.path("workflow_id").asText(""));

            // Check if conversation is cancelled
            List<String> statusList = mgr.jdbc.queryForList(
                    "SELECT status FROM conversations WHERE conversation_id = :id::uuid",
                    Map.of("id", conversationId), String.class);
            if (!statusList.isEmpty() && "cancelled".equals(statusList.get(0))) {
                log.info("Conversation " + conversationId + " cancelled - giving up for " + imageUuid);
                completeTx(imageUuid, msg, "give_up", null);
                return;
            }

            Budget budget = loadBudget(sessionUuid);
            String scoringMsg = buildScoringMessage(imageUuid, msg, budget);
            saveChatMessage(workflowId, "user", scoringMsg);

            boolean thinkFirst = shouldThink(msg, budget);
            List<ObjectNode> messages = buildConversation(conversationId, scoringMsg);
            JsonNode decision = runReasoningLoop(messages, thinkFirst);

            if (decision != null && "escalate".equals(decision.path("decision").asText("")) && !thinkFirst) {
                log.info("Escalate with thinking=off - retrying with thinking for " + imageUuid);
                JsonNode second = runReasoningLoop(buildConversation(conversationId, null), true);
                if (second != null) decision = second;
            }

            if (decision == null || !decision.has("decision")) {
                decision = heuristicDecision(msg, budget);
            }

            String action  = decision.path("decision").asText("give_up");
            String message = ConversationAgentManager.nullIfBlank(decision.path("message").asText(""));

            handleVlmEvalPrompt(decision);

            if (message != null) {
                saveChatMessage(workflowId, "assistant", message);
                mgr.chatBroadcast.sendMessage(conversationId, message);
            }

            final String finalAction = action;
            requiresNew.execute(s -> {
                mgr.jdbc.update(
                        "UPDATE pending_decisions SET decision = :decision WHERE image_uuid = :id::uuid",
                        Map.of("decision", finalAction, "id", imageUuid));
                return null;
            });

            completeTx(imageUuid, msg, action, decision);

            if ("escalate".equals(action)) {
                initiateShutdown();
                return;
            }

            double confidence = decision.path("confidence").asDouble(0.0);
            String reasoning  = decision.path("reasoning").asText("");
            mgr.galleryBroadcast.decision(imageUuid, action, reasoning, confidence, workflowId, conversationId);

        } catch (Exception e) {
            log.warning("processVerdict error for " + imageUuid + ": " + e.getMessage());
        }
    }

    private void completeTx(String imageUuid, JsonNode msg, String action, JsonNode decision) {
        requiresNew.execute(s -> {
            try {
                mgr.jdbc.update("DELETE FROM pending_decisions WHERE image_uuid = :id::uuid",
                        Map.of("id", imageUuid));

                String sessionUuid    = msg.path("session_uuid").asText();
                String workflowId     = ConversationAgentManager.nullIfBlank(msg.path("workflow_id").asText(""));

                mgr.jdbc.update(
                        "UPDATE images SET decision = :decision WHERE image_uuid = :id::uuid",
                        Map.of("decision", action, "id", imageUuid));

                switch (action) {
                    case "accept" -> log.info("Accepted " + imageUuid);

                    case "retry", "inpaint", "generate" -> {
                        if ("inpaint".equals(action)) {
                            log.info("Inpaint not implemented - downgrading to retry for " + imageUuid);
                        }
                        Budget budget = loadBudget(sessionUuid);
                        if (budget.retriesUsed() >= budget.maxRetries()) {
                            mgr.jdbc.update("UPDATE images SET decision = 'give_up' WHERE image_uuid = :id::uuid",
                                    Map.of("id", imageUuid));
                            log.info("Retry budget exhausted - giving up for " + imageUuid);
                            break;
                        }
                        incrementBudget(sessionUuid, "retries_used");

                        String newImageUuid = UUID.randomUUID().toString();
                        int    newSeq       = msg.path("sequence_number").asInt(0) + 1;
                        String retryPrompt  = decision != null
                                ? decision.path("retry_prompt").asText(msg.path("prompt").asText(""))
                                : msg.path("prompt").asText("");
                        JsonNode retryParams   = decision != null && decision.has("retry_params")
                                ? decision.get("retry_params") : mgr.mapper.createObjectNode();
                        JsonNode retryGraphOps = decision != null && decision.has("retry_graph_ops")
                                ? decision.get("retry_graph_ops") : mgr.mapper.createArrayNode();
                        JsonNode retryLoras    = decision != null && decision.has("retry_loras")
                                ? decision.get("retry_loras") : mgr.mapper.createArrayNode();
                        String retryModel      = decision != null
                                ? ConversationAgentManager.nullIfBlank(decision.path("retry_model").asText("")) : null;

                        ObjectNode baseParams   = (ObjectNode) msg.path("scores").path("workflow_params");
                        ObjectNode mergedParams = baseParams != null && !baseParams.isMissingNode()
                                ? baseParams.deepCopy() : mgr.mapper.createObjectNode();
                        if (retryParams.isObject()) {
                            retryParams.fields().forEachRemaining(e -> mergedParams.set(e.getKey(), e.getValue()));
                        }

                        ObjectNode retryMsg = mgr.mapper.createObjectNode();
                        retryMsg.put("image_uuid",       newImageUuid);
                        retryMsg.put("session_uuid",     sessionUuid);
                        retryMsg.put("sequence_number",  newSeq);
                        retryMsg.put("workflow_path",    msg.path("workflow_path").asText(""));
                        retryMsg.put("prompt",           retryPrompt);
                        retryMsg.put("workflow_id",      workflowId != null ? workflowId : "");
                        retryMsg.put("conversation_id",  conversationId);
                        if (retryModel != null) retryMsg.put("model_type", retryModel);
                        retryMsg.set("workflow_params",  mergedParams);
                        retryMsg.set("graph_ops",        retryGraphOps);
                        retryMsg.set("loras",            retryLoras);

                        mgr.jmsTemplate.convertAndSend("loop.retry", mgr.mapper.writeValueAsString(retryMsg));
                        log.info("Retry queued: " + imageUuid + " -> " + newImageUuid + " seq=" + newSeq);
                    }

                    case "give_up" -> {
                        String reasoning = decision != null ? decision.path("reasoning").asText("") : "unknown";
                        log.info("Give up for " + imageUuid + ": " + reasoning);
                    }

                    case "escalate" -> {
                        String reason = decision != null ? decision.path("reasoning").asText("") : "";
                        mgr.jdbc.update(
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

    // -- Generation failure feedback --------------------------------------------

    private void processGenerationFailed(String imageUuid, String reason) {
        try {
            String feedbackMsg =
                    "[GENERATION FAILED]\n" +
                    "image_uuid: " + imageUuid + "\n\n" +
                    "Your generation attempt was rejected before producing an image.\n" +
                    "Reason: " + reason + "\n\n" +
                    "Diagnose the problem and recover: check what models and workflows are " +
                    "actually installed (get_available_models), identify what is missing, " +
                    "and follow the appropriate protocol to fix it before retrying.";

            saveChatMessage(null, "user", feedbackMsg);
            mgr.chatBroadcast.sendChunk(conversationId,
                    "Generation failed: " + reason + " Diagnosing...");
            mgr.chatBroadcast.sendDone(conversationId);

            List<ObjectNode> messages = buildConversation(conversationId);
            JsonNode decision = runReasoningLoop(messages, false);
            if (decision == null) {
                mgr.chatBroadcast.sendError(conversationId,
                        "Generation failed and the model did not respond. Please try again.");
                return;
            }
            String message = ConversationAgentManager.nullIfBlank(decision.path("message").asText(""));
            String action  = decision.path("decision").asText("await_input");
            if ("generate".equals(action)) {
                String ckptError = validateCheckpointGraphOps(decision);
                if (ckptError != null) {
                    messages.add(mgr.mapper.createObjectNode().put("role", "user").put("content", ckptError));
                    decision = runReasoningLoop(messages, false);
                    if (decision != null) {
                        message = ConversationAgentManager.nullIfBlank(decision.path("message").asText(""));
                        action  = decision.path("decision").asText("await_input");
                    }
                }
                if ("generate".equals(action)) {
                    Map<String, String> state = queueNewGeneration(decision);
                    if (state != null && message == null) message = "On it. Generation submitted.";
                }
            }
            if (message != null) {
                saveChatMessage(null, "assistant", message);
                mgr.chatBroadcast.sendChunk(conversationId, message);
                mgr.chatBroadcast.sendDone(conversationId);
            }
        } catch (Exception e) {
            log.warning("processGenerationFailed error for " + conversationId + ": " + e.getMessage());
        }
    }

    // -- Reasoning loop ---------------------------------------------------------

    private JsonNode runReasoningLoop(List<ObjectNode> messages, boolean thinkingEnabled) {
        int maxTokens = thinkingEnabled ? mgr.cfg.getMaxTokensThinking() : mgr.cfg.getMaxTokens();
        ArrayNode tools = buildToolDefinitions();
        List<ObjectNode> conversation = new ArrayList<>(messages);
        boolean forceToolUse = false;

        for (int i = 0; i < SAFETY_LIMIT; i++) {
            boolean lastChance = (i == SAFETY_LIMIT - 1);

            ObjectNode body = mgr.mapper.createObjectNode();
            body.put("model",       mgr.resolvedModelId);
            body.put("temperature", mgr.cfg.getTemperature());
            body.put("max_tokens",  maxTokens);
            if (!lastChance) {
                body.set("tools", tools);
                if (forceToolUse) body.put("tool_choice", "required");
            }

            List<ObjectNode> prompt = new ArrayList<>(conversation);
            if (lastChance) {
                prompt.add(mgr.mapper.createObjectNode()
                        .put("role", "user")
                        .put("content", "Respond now with your JSON decision object only. /no_think"));
            } else {
                // Append thinking-mode suffix to the last user message so Qwen3 knows
                // whether to emit a <think> block. Without this it defaults to thinking
                // mode and frequently exhausts max_tokens before producing any output.
                String thinkSuffix = thinkingEnabled ? " /think" : " /no_think";
                for (int j = prompt.size() - 1; j >= 0; j--) {
                    if ("user".equals(prompt.get(j).path("role").asText(""))) {
                        ObjectNode patched = prompt.get(j).deepCopy();
                        patched.put("content", patched.path("content").asText("") + thinkSuffix);
                        prompt.set(j, patched);
                        break;
                    }
                }
            }

            ArrayNode msgArray = body.putArray("messages");
            for (ObjectNode m : prompt) msgArray.add(m);

            ObjectNode response;
            try {
                response = mgr.llmClient.post()
                        .uri("/chat/completions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(mgr.mapper.writeValueAsBytes(body))
                        .retrieve()
                        .body(ObjectNode.class);
            } catch (Exception e) {
                log.warning("LLM call failed (iter " + i + "): " + e.getMessage());
                if (isTimeoutException(e)) mgr.scheduleLlmRestart();
                return null;
            }

            JsonNode choice  = response.path("choices").path(0);
            JsonNode message = choice.path("message");

            conversation.add((ObjectNode) message.deepCopy());

            // Check for tool calls by presence in the message, not finish_reason  - 
            // local model servers often return finish_reason:"stop" even for tool calls.
            JsonNode toolCallsNode = message.path("tool_calls");
            if (toolCallsNode.isArray() && toolCallsNode.size() > 0) {
                forceToolUse = false;
                for (JsonNode toolCall : toolCallsNode) {
                    String toolCallId = toolCall.path("id").asText();
                    String toolName   = toolCall.path("function").path("name").asText();
                    String argsJson   = toolCall.path("function").path("arguments").asText("{}");
                    String result     = executeToolCall(toolName, argsJson, conversation);

                    ObjectNode toolResult = mgr.mapper.createObjectNode();
                    toolResult.put("role",        "tool");
                    toolResult.put("tool_call_id", toolCallId);
                    toolResult.put("content",      result);
                    conversation.add(toolResult);
                }
                continue;
            }

            String raw    = message.path("content").asText("").strip();
            JsonNode parsed = parseDecisionJson(raw);
            if (parsed != null) return parsed;

            if (!raw.isEmpty()) {
                // Model produced plain text instead of a tool call or JSON decision.
                // Inject a nudge and force tool_choice:required on the next call.
                if (!lastChance) {
                    log.warning("LLM narrated instead of acting (" + raw.substring(0, Math.min(60, raw.length())) + "...) - forcing tool call");
                    forceToolUse = true;
                    conversation.add(mgr.mapper.createObjectNode()
                            .put("role", "user")
                            .put("content", "Call the appropriate tool now, or output your JSON decision."));
                    continue;
                }
                // Last chance exhausted - surface as await_input so the user sees it.
                return mgr.mapper.createObjectNode()
                        .put("decision",   "await_input")
                        .put("message",    raw)
                        .put("confidence", 0.5);
            }
            return null;
        }

        log.warning("Safety limit (" + SAFETY_LIMIT + ") reached for " + conversationId);
        return null;
    }

    // -- Tool definitions -------------------------------------------------------

    private ArrayNode buildToolDefinitions() {
        ArrayNode tools = mgr.mapper.createArrayNode();
        tools.add(buildTool("analyze_image",
                "Ask the VLM a free-form question about the current candidate image. " +
                "Use image_path from your context as image_url.",
                Map.of("image_url", "file:// URI of the image",
                       "question",  "Your specific question about the image"),
                List.of("image_url", "question")));

        tools.add(buildTool("get_available_models",
                "Query ComfyUI for installed checkpoints, LoRAs, ControlNets, VAEs, UNETs, " +
                "and available workflow files.",
                Map.of(), List.of()));

        tools.add(buildTool("get_workflow",
                "Read the JSON graph of a workflow file by name.",
                Map.of("name", "Workflow filename, e.g. sdxl_base.json"),
                List.of("name")));

        tools.add(buildTool("install_model",
                "Download a model from HuggingFace into ComfyUI's model directories. " +
                "ALWAYS get user approval via await_input before calling this. " +
                "After calling, immediately call wait_for_download(filename).",
                Map.of("repo_id",    "HuggingFace repo ID",
                       "filename",   "Filename to download",
                       "model_type", "checkpoint | lora | vae | unet | clip | t5"),
                List.of("repo_id", "filename", "model_type")));

        tools.add(buildTool("wait_for_download",
                "Wait for a model file to finish downloading. Call immediately after install_model. " +
                "Blocks until the file appears (polls every 15s, times out after 10 minutes).",
                Map.of("filename", "The filename passed to install_model"),
                List.of("filename")));

        tools.add(buildTool("search_web",
                "Search the web for factual information. Use before generating whenever the user " +
                "names a specific character, person, place, or real-world subject whose visual " +
                "details you are not certain of.",
                Map.of("query", "Concise search query"),
                List.of("query")));

        tools.add(buildTool("get_system_prompt",
                "Read the current system prompt from disk.",
                Map.of(), List.of()));

        tools.add(buildTool("update_system_prompt",
                "Rewrite your own system prompt. Write the COMPLETE new prompt. " +
                "Call get_system_prompt afterward to verify.",
                Map.of("content", "The complete new system prompt text"),
                List.of("content")));

        tools.add(buildTool("check_system_status",
                "Check the health of the pipeline: ComfyUI connectivity and queue depth, " +
                "pending generations, and recent warnings/errors from the pipeline log. " +
                "Call this whenever something seems wrong or a tool call fails unexpectedly.",
                Map.of(), List.of()));

        return tools;
    }

    private ObjectNode buildTool(String name, String description,
                                  Map<String, String> propDescriptions, List<String> required) {
        ObjectNode tool = mgr.mapper.createObjectNode();
        tool.put("type", "function");
        ObjectNode fn = mgr.mapper.createObjectNode();
        fn.put("name", name);
        fn.put("description", description);
        ObjectNode params = mgr.mapper.createObjectNode();
        params.put("type", "object");
        ObjectNode props = mgr.mapper.createObjectNode();
        propDescriptions.forEach((k, v) -> {
            ObjectNode p = mgr.mapper.createObjectNode();
            p.put("type", "string");
            p.put("description", v);
            props.set(k, p);
        });
        params.set("properties", props);
        ArrayNode req = mgr.mapper.createArrayNode();
        required.forEach(req::add);
        params.set("required", req);
        fn.set("parameters", params);
        tool.set("function", fn);
        return tool;
    }

    // -- Tool execution ---------------------------------------------------------

    private String executeToolCall(String toolName, String argsJson, List<ObjectNode> conversation) {
        return switch (toolName) {
            case "analyze_image"      -> executeAnalyzeImage(argsJson);
            case "get_available_models" -> executeGetAvailableModels();
            case "get_workflow"       -> executeGetWorkflow(argsJson);
            case "install_model"      -> executeInstallModel(argsJson);
            case "wait_for_download"  -> executeWaitForDownload(argsJson);
            case "search_web"         -> executeSearchWeb(argsJson);
            case "get_system_prompt"  -> readSystemPrompt();
            case "update_system_prompt" -> executeUpdateSystemPrompt(argsJson, conversation);
            case "check_system_status"  -> executeCheckSystemStatus();
            default -> "Unknown tool: " + toolName;
        };
    }

    private String executeAnalyzeImage(String argsJson) {
        try {
            JsonNode args = mgr.mapper.readTree(argsJson);
            ObjectNode req = mgr.mapper.createObjectNode();
            req.put("image_url", args.path("image_url").asText(""));
            req.put("question",  args.path("question").asText(""));
            JsonNode result = mgr.vlmClient.post().uri("/analyze")
                    .contentType(MediaType.APPLICATION_JSON).body(req)
                    .retrieve().body(JsonNode.class);
            return result.path("answer").asText("(no answer)");
        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }

    private Set<String> fetchInstalledCheckpoints() {
        try {
            JsonNode info = mgr.comfyuiClient.get().uri("/object_info/CheckpointLoaderSimple")
                    .retrieve().body(JsonNode.class);
            JsonNode list = info.path("CheckpointLoaderSimple").path("input")
                    .path("required").path("ckpt_name").path(0);
            Set<String> names = new java.util.HashSet<>();
            if (list.isArray()) list.forEach(n -> names.add(n.asText()));
            return names;
        } catch (Exception e) {
            return Set.of();
        }
    }

    private String validateCheckpointGraphOps(JsonNode decision) {
        JsonNode graphOps = decision.path("generate_graph_ops");

        // Check whether the model specified a checkpoint in graph_ops
        String requestedCheckpoint = null;
        if (graphOps.isArray()) {
            for (JsonNode op : graphOps) {
                if (!"set_input".equals(op.path("op").asText())) continue;
                if (!"ckpt_name".equals(op.path("input").asText())) continue;
                String v = op.path("value").asText("");
                if (!v.isBlank()) { requestedCheckpoint = v; break; }
            }
        }

        Set<String> installed = fetchInstalledCheckpoints();

        if (requestedCheckpoint != null && !installed.contains(requestedCheckpoint)) {
            return "[GENERATION ERROR]\n" +
                   "Checkpoint '" + requestedCheckpoint + "' is not installed.\n" +
                   "Installed checkpoints: " + installed + "\n" +
                   "Call get_available_models() to see what is actually available, " +
                   "then retry your generate decision using a set_input with an installed checkpoint name.";
        }

        if (installed.isEmpty()) {
            return "[GENERATION ERROR]\n" +
                   "No checkpoints are installed. You cannot generate without one.\n" +
                   "Follow the install_model protocol: call get_available_models() to confirm, " +
                   "call search_web to find the right checkpoint for the user's creative goal, " +
                   "propose ONE specific model with repo_id and filename, then emit await_input " +
                   "to get explicit user approval before calling install_model.";
        }

        return null;
    }

    private String executeCheckSystemStatus() {
        StringBuilder sb = new StringBuilder("=== SYSTEM STATUS ===\n\n");

        // ComfyUI health and queue
        try {
            mgr.comfyuiClient.get().uri("/system_stats").retrieve().body(JsonNode.class);
            sb.append("ComfyUI: UP\n");
            try {
                JsonNode queue = mgr.comfyuiClient.get().uri("/queue").retrieve().body(JsonNode.class);
                sb.append("  Queue running: ").append(queue.path("queue_running").size()).append("\n");
                sb.append("  Queue pending: ").append(queue.path("queue_pending").size()).append("\n");
            } catch (Exception e) {
                sb.append("  Queue status unavailable: ").append(e.getMessage()).append("\n");
            }
        } catch (Exception e) {
            sb.append("ComfyUI: DOWN - ").append(e.getMessage()).append("\n");
            sb.append("  This means no images can be generated until ComfyUI is restarted.\n");
        }

        // Pending generations in DB
        try {
            Integer pending = mgr.jdbc.queryForObject(
                    "SELECT COUNT(*) FROM pending_generations", Map.of(), Integer.class);
            sb.append("Pending generations in DB: ").append(pending).append("\n");
            if (pending != null && pending > 0) {
                List<Map<String, Object>> stuck = mgr.jdbc.queryForList(
                        "SELECT image_uuid::text, created_at FROM pending_generations ORDER BY created_at LIMIT 3",
                        Map.of());
                stuck.forEach(r -> sb.append("  - ").append(r.get("image_uuid"))
                        .append(" (since ").append(r.get("created_at")).append(")\n"));
            }
        } catch (Exception e) {
            sb.append("DB pending_generations unavailable: ").append(e.getMessage()).append("\n");
        }

        // Recent pipeline log warnings/errors
        try {
            Path logFile = Path.of("/tmp/ai-loop/pipeline.log");
            if (Files.exists(logFile)) {
                List<String> lines = Files.readAllLines(logFile);
                List<String> errors = lines.stream()
                        .filter(l -> l.contains(" WARN ") || l.contains(" ERROR "))
                        .collect(Collectors.toList());
                int start = Math.max(0, errors.size() - 5);
                sb.append("Recent pipeline warnings/errors:\n");
                if (errors.isEmpty()) {
                    sb.append("  (none)\n");
                } else {
                    errors.subList(start, errors.size())
                            .forEach(l -> sb.append("  ").append(l.trim()).append("\n"));
                }
            } else {
                sb.append("Pipeline log not found at /tmp/ai-loop/pipeline.log\n");
            }
        } catch (Exception e) {
            sb.append("Pipeline log read error: ").append(e.getMessage()).append("\n");
        }

        return sb.toString();
    }

    private String executeGetAvailableModels() {
        try {
            StringBuilder sb = new StringBuilder();
            appendModelList(sb, "Checkpoints", "/object_info/CheckpointLoaderSimple",
                    "CheckpointLoaderSimple", "ckpt_name",
                    "  (none - install a checkpoint via install_model)\n");
            appendModelList(sb, "LoRAs",       "/object_info/LoraLoader",
                    "LoraLoader",             "lora_name",   "  (none installed)\n");
            appendModelList(sb, "ControlNets", "/object_info/ControlNetLoader",
                    "ControlNetLoader",       "control_net_name", "  (none installed)\n");
            appendModelList(sb, "VAEs",        "/object_info/VAELoader",
                    "VAELoader",              "vae_name",    "  (none installed)\n");
            appendModelList(sb, "UNETs (Flux)", "/object_info/UNETLoader",
                    "UNETLoader",             "unet_name",   null);
            sb.append("Available workflows: ").append(
                    listWorkflowNames().isEmpty() ? "(none)" : String.join(", ", listWorkflowNames())).append("\n");
            return sb.toString();
        } catch (Exception e) {
            return "Error querying models: " + e.getMessage();
        }
    }

    private void appendModelList(StringBuilder sb, String label, String uri,
                                  String nodeClass, String inputField, String emptyMsg) {
        try {
            JsonNode info = mgr.comfyuiClient.get().uri(uri).retrieve().body(JsonNode.class);
            JsonNode list = info.path(nodeClass).path("input").path("required").path(inputField).path(0);
            if (list.isArray() && list.size() > 0) {
                sb.append(label).append(":\n");
                for (JsonNode item : list) sb.append("  ").append(item.asText()).append("\n");
            } else if (emptyMsg != null) {
                sb.append(label).append(":\n").append(emptyMsg);
            }
        } catch (Exception e) {
            if (emptyMsg != null) sb.append(label).append(": (unreachable)\n");
        }
    }

    private String executeGetWorkflow(String argsJson) {
        try {
            JsonNode args = mgr.mapper.readTree(argsJson);
            String name = args.path("name").asText("").strip();
            if (name.isEmpty()) return "Error: name is required";
            Path wfPath = mgr.workflowsDir.resolve(name);
            if (!Files.exists(wfPath)) {
                return "Workflow '" + name + "' not found. Available: " + String.join(", ", listWorkflowNames());
            }
            JsonNode wf = mgr.mapper.readTree(Files.readString(wfPath));
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
                node.path("inputs").fields().forEachRemaining(e -> {
                    if (!e.getValue().isArray())
                        sb.append(String.format("         %s = %s%n", e.getKey(), e.getValue().asText()));
                });
            }
            return sb.toString();
        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }

    private String executeInstallModel(String argsJson) {
        try {
            JsonNode args    = mgr.mapper.readTree(argsJson);
            String repoId    = args.path("repo_id").asText("").strip();
            String filename  = args.path("filename").asText("").strip();
            String modelType = args.path("model_type").asText("checkpoint").strip();
            if (repoId.isEmpty() || filename.isEmpty()) return "Error: repo_id and filename are required";

            // Validate file exists on HuggingFace before starting download
            String hfUrl = "https://huggingface.co/" + repoId + "/resolve/main/" + filename;
            try {
                HttpClient hc = HttpClient.newBuilder()
                        .followRedirects(HttpClient.Redirect.ALWAYS)
                        .connectTimeout(Duration.ofSeconds(15))
                        .build();
                HttpResponse<Void> resp = hc.send(
                        HttpRequest.newBuilder()
                                .method("HEAD", HttpRequest.BodyPublishers.noBody())
                                .uri(URI.create(hfUrl))
                                .timeout(Duration.ofSeconds(30))
                                .build(),
                        HttpResponse.BodyHandlers.discarding());
                int status = resp.statusCode();
                if (status == 404) {
                    return "Error: file not found on HuggingFace - " + repoId + "/" + filename +
                           " returned 404. Call search_web to find the correct repo and filename.";
                }
                if (status >= 400) {
                    return "Error: HuggingFace returned HTTP " + status + " for " + repoId + "/" + filename +
                           ". Call search_web to verify the correct repo and filename.";
                }
                long contentLength = resp.headers().firstValue("content-length")
                        .map(Long::parseLong).orElse(-1L);
                if (contentLength > 0 && contentLength < 1_000_000) {
                    return "Error: " + repoId + "/" + filename + " is only " + contentLength +
                           " bytes - this is not a model file (pointer or error page). " +
                           "Call search_web to find the correct filename.";
                }
                downloadExpectedSizes.put(filename, contentLength);
            } catch (Exception e) {
                log.warning("Pre-download HEAD check failed for " + repoId + "/" + filename + ": " + e.getMessage());
                // Continue anyway - network issue, not a bad file
            }

            String subdir = switch (modelType) {
                case "lora"  -> "loras";
                case "vae"   -> "vae";
                case "unet"  -> "unet";
                case "clip"  -> "clip";
                case "t5"    -> "t5";
                default      -> "checkpoints";
            };
            Path destDir = mgr.comfyuiModelsDir.resolve(subdir);
            Files.createDirectories(destDir);

            downloadDestDirs.put(filename, destDir);

            new ProcessBuilder("hf", "download", repoId, filename, "--local-dir", destDir.toString())
                    .redirectOutput(new java.io.File("/tmp/ai-loop/model-download.log"))
                    .redirectErrorStream(true)
                    .start();

            log.info("Model download started: " + repoId + "/" + filename + " -> " + destDir);
            mgr.chatBroadcast.sendChunk(conversationId,
                    "Downloading **" + filename + "** from `" + repoId + "`...");
            return "Download started: " + filename + " -> " + destDir +
                   ". Call wait_for_download(\"" + filename + "\") now.";
        } catch (Exception e) {
            return "Error starting download: " + e.getMessage();
        }
    }

    private String executeWaitForDownload(String argsJson) {
        try {
            JsonNode args = mgr.mapper.readTree(argsJson);
            String filename = args.path("filename").asText("").strip();
            if (filename.isEmpty()) return "Error: filename is required";

            long deadlineMs = System.currentTimeMillis() + 10 * 60 * 1000L;
            int pollCount = 0;
            while (System.currentTimeMillis() < deadlineMs) {
                try (var stream = Files.walk(mgr.comfyuiModelsDir, 3)) {
                    Optional<Path> found = stream
                            .filter(p -> p.getFileName().toString().equals(filename))
                            .findFirst();
                    if (found.isPresent() && found.get().toFile().length() > 10_000_000) {
                        downloadExpectedSizes.remove(filename);
                        downloadDestDirs.remove(filename);
                        log.info("wait_for_download: " + filename + " ready");
                        mgr.chatBroadcast.sendChunk(conversationId, "**" + filename + "** is ready.");
                        return "Ready: " + filename + " found. Call get_available_models() and proceed.";
                    }
                } catch (Exception e) {
                    log.warning("wait_for_download scan: " + e.getMessage());
                }
                pollCount++;
                mgr.chatBroadcast.sendProgress(conversationId,
                        buildProgressText(filename, pollCount * 5L));
                Thread.sleep(5_000);
            }
            return "Timeout: " + filename + " did not appear within 10 minutes.";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return "Interrupted while waiting for download.";
        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }

    private String buildProgressText(String filename, long elapsedSec) {
        Path destDir = downloadDestDirs.get(filename);
        long total = downloadExpectedSizes.getOrDefault(filename, 0L);
        long downloaded = 0;

        if (destDir != null) {
            Path cacheDir = destDir.resolve(".cache/huggingface/download");
            if (Files.isDirectory(cacheDir)) {
                try (var ls = Files.list(cacheDir)) {
                    Optional<Path> partial = ls
                            .filter(p -> {
                                String n = p.getFileName().toString();
                                return n.startsWith(filename + ".") && n.endsWith(".incomplete");
                            })
                            .findFirst();
                    if (partial.isPresent()) downloaded = Files.size(partial.get());
                } catch (Exception ignored) {}
            }
        }

        if (total > 0 && downloaded > 0) {
            double pct = Math.min(100.0, (double) downloaded / total * 100.0);
            int filled = (int) (pct / 5.0);
            String bar = "#".repeat(filled) + ".".repeat(20 - filled);
            return String.format("**%s** - %s / %s [%s] %.0f%%",
                    filename, humanBytes(downloaded), humanBytes(total), bar, pct);
        } else if (total > 0) {
            return String.format("**%s** - %s total, %ds elapsed",
                    filename, humanBytes(total), elapsedSec);
        } else {
            return String.format("**%s** - %ds elapsed", filename, elapsedSec);
        }
    }

    private static String humanBytes(long bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1_048_576L) return String.format("%.1f KB", bytes / 1024.0);
        if (bytes < 1_073_741_824L) return String.format("%.1f MB", bytes / 1_048_576.0);
        return String.format("%.2f GB", bytes / 1_073_741_824.0);
    }

    private String executeSearchWeb(String argsJson) {
        try {
            JsonNode args = mgr.mapper.readTree(argsJson);
            String query = args.path("query").asText("").strip();
            if (query.isEmpty()) return "Error: query is required";

            JsonNode result = mgr.searxngClient.get()
                    .uri(u -> u.path("/search")
                            .queryParam("q", query)
                            .queryParam("format", "json")
                            .queryParam("language", "en")
                            .queryParam("categories", "general")
                            .build())
                    .retrieve().body(JsonNode.class);

            JsonNode results = result.path("results");
            if (!results.isArray() || results.isEmpty()) return "No results found for: " + query;

            StringBuilder sb = new StringBuilder("Web search results for: ").append(query).append("\n\n");
            int count = Math.min(5, results.size());
            for (int i = 0; i < count; i++) {
                JsonNode r = results.get(i);
                sb.append(i + 1).append(". ").append(r.path("title").asText("")).append("\n");
                sb.append("   ").append(r.path("url").asText("")).append("\n");
                String content = r.path("content").asText("").strip();
                if (!content.isEmpty()) sb.append("   ").append(content).append("\n");
                sb.append("\n");
            }
            return sb.toString();
        } catch (Exception e) {
            log.warning("search_web failed: " + e.getMessage());
            return "Search failed: " + e.getMessage();
        }
    }

    private String executeUpdateSystemPrompt(String argsJson, List<ObjectNode> conversation) {
        try {
            JsonNode args = mgr.mapper.readTree(argsJson);
            String content = args.path("content").asText("").strip();
            if (content.isEmpty()) return "Error: content is required";
            Files.writeString(mgr.systemPromptPath, content);
            log.info("System prompt updated (" + content.length() + " chars)");
            if (!conversation.isEmpty()) {
                String ctx = conversation.get(0).path("content").asText("");
                int sep = ctx.indexOf("\n\n=== CURRENT CONTEXT");
                conversation.get(0).put("content", sep >= 0 ? content + ctx.substring(sep) : content);
            }
            return "System prompt updated. Call get_system_prompt to verify.";
        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }

    // -- Conversation building --------------------------------------------------

    private List<ObjectNode> buildConversation(String convId) {
        return buildConversation(convId, null);
    }

    private List<ObjectNode> buildConversation(String convId, String extraUserMessage) {
        String system = readSystemPrompt() + "\n\n" + buildContextBlock();
        String memory = loadConversationMemory(convId);
        if (memory != null) system += "\n\n" + memory;
        List<ObjectNode> messages = new ArrayList<>();
        messages.add(mgr.mapper.createObjectNode().put("role", "system").put("content", system));

        if (convId != null) {
            List<Map<String, Object>> rows = mgr.jdbc.queryForList(
                    "SELECT role, content FROM (" +
                    "  SELECT role, content, created_at FROM chat_messages " +
                    "  WHERE conversation_id = :convId::uuid " +
                    "  ORDER BY created_at DESC LIMIT 60" +
                    ") sub ORDER BY created_at ASC",
                    Map.of("convId", convId));
            for (Map<String, Object> row : rows) {
                messages.add(mgr.mapper.createObjectNode()
                        .put("role",    (String) row.get("role"))
                        .put("content", (String) row.get("content")));
            }
        }

        if (extraUserMessage != null) {
            messages.add(mgr.mapper.createObjectNode().put("role", "user").put("content", extraUserMessage));
        }
        return messages;
    }

    private String buildContextBlock() {
        StringBuilder sb = new StringBuilder("=== CURRENT CONTEXT =============================================\n");

        String northStar = mgr.contextService.northStar();
        if (northStar != null) sb.append("North star:\n").append(northStar).append("\n\n");

        String wfId = workflowState != null ? workflowState.get("workflow_id") : null;
        String sessionDir = mgr.contextService.sessionDirection(wfId);
        if (sessionDir != null) sb.append("Session direction:\n").append(sessionDir).append("\n\n");

        String taste = mgr.contextService.tasteSynthesis(wfId);
        if (taste != null) sb.append("Taste synthesis:\n").append(taste).append("\n\n");

        List<String> workflows = listWorkflowNames();
        sb.append("Available workflows: ").append(
                workflows.isEmpty() ? "(none in loop/workflows/)" : String.join(", ", workflows)).append("\n");
        return sb.toString();
    }

    // -- New generation ---------------------------------------------------------

    private Map<String, String> queueNewGeneration(JsonNode decision) {
        try {
            String workflowName = ConversationAgentManager.nullIfBlank(decision.path("generate_workflow").asText(""));
            if (workflowName == null) {
                List<String> available = listWorkflowNames();
                if (available.isEmpty()) { log.warning("No workflows available"); return null; }
                workflowName = available.get(0);
            }
            Path workflowPath = mgr.workflowsDir.resolve(workflowName);
            if (!Files.exists(workflowPath)) { log.warning("Workflow not found: " + workflowPath); return null; }

            String prompt    = decision.path("generate_prompt").asText("").strip();
            String modelType = decision.path("generate_model").asText("auto");
            JsonNode params  = decision.has("generate_params")    ? decision.get("generate_params")    : mgr.mapper.createObjectNode();
            JsonNode loras   = decision.has("generate_loras")     ? decision.get("generate_loras")     : mgr.mapper.createArrayNode();
            JsonNode graphOps = decision.has("generate_graph_ops") ? decision.get("generate_graph_ops") : mgr.mapper.createArrayNode();

            if (workflowState == null || !workflowPath.toString().equals(workflowState.get("workflow_path"))) {
                String label = workflowName.replace(".json", "").replace("_", " ");
                label = Character.toUpperCase(label.charAt(0)) + label.substring(1);
                String wfId = createWorkflow(label, workflowPath.toString(), 3, 2);
                if (wfId == null) return null;

                String sessionUuid = UUID.randomUUID().toString();
                mgr.jdbc.update(
                        "INSERT INTO budget (session_uuid, workflow_id, conversation_id, " +
                        "max_retries, max_inpaints, expires_at) " +
                        "VALUES (:sess::uuid, :wfId::uuid, :convId::uuid, :maxRetries, :maxInpaints, " +
                        "now() + interval '24 hours')",
                        new MapSqlParameterSource()
                                .addValue("sess",       sessionUuid)
                                .addValue("wfId",       wfId)
                                .addValue("convId",     conversationId)
                                .addValue("maxRetries", 3)
                                .addValue("maxInpaints", 2));

                workflowState = Map.of(
                        "workflow_id",   wfId,
                        "session_uuid",  sessionUuid,
                        "workflow_path", workflowPath.toString());
            }

            String imageUuid = UUID.randomUUID().toString();
            ObjectNode genMsg = mgr.mapper.createObjectNode();
            genMsg.put("image_uuid",      imageUuid);
            genMsg.put("session_uuid",    workflowState.get("session_uuid"));
            genMsg.put("sequence_number", 0);
            genMsg.put("workflow_path",   workflowState.get("workflow_path"));
            genMsg.put("workflow_id",     workflowState.get("workflow_id"));
            genMsg.put("conversation_id", conversationId);
            genMsg.put("prompt",          prompt);
            genMsg.put("model_type",      modelType);
            genMsg.set("workflow_params", params);
            genMsg.set("graph_ops",       graphOps);
            genMsg.set("loras",           loras);

            mgr.jmsTemplate.convertAndSend("loop.generate", mgr.mapper.writeValueAsString(genMsg));
            log.info("Generation queued: " + imageUuid + " for conversation " + conversationId);
            return workflowState;
        } catch (Exception e) {
            log.warning("queueNewGeneration failed: " + e.getMessage());
            return null;
        }
    }

    private String createWorkflow(String name, String workflowPath, int maxRetries, int maxInpaints) {
        try {
            List<String> ids = mgr.jdbc.queryForList(
                    "INSERT INTO workflows (conversation_id, name, workflow_path, max_retries, max_inpaints) " +
                    "VALUES (:convId::uuid, :name, :path, :maxRetries, :maxInpaints) RETURNING workflow_id::text",
                    new MapSqlParameterSource()
                            .addValue("convId",     conversationId)
                            .addValue("name",       name)
                            .addValue("path",       workflowPath)
                            .addValue("maxRetries", maxRetries)
                            .addValue("maxInpaints", maxInpaints),
                    String.class);
            return ids.isEmpty() ? null : ids.get(0);
        } catch (Exception e) {
            log.warning("createWorkflow failed: " + e.getMessage());
            return null;
        }
    }

    // -- VLM eval prompt --------------------------------------------------------

    private void handleVlmEvalPrompt(JsonNode decision) {
        String evalPrompt = ConversationAgentManager.nullIfBlank(decision.path("vlm_eval_prompt").asText(""));
        if (evalPrompt == null) return;
        try {
            mgr.jdbc.update(
                    "INSERT INTO session_vlm_prompts (conversation_id, eval_prompt) " +
                    "VALUES (:convId::uuid, :prompt)",
                    Map.of("convId", conversationId, "prompt", evalPrompt));
        } catch (Exception e) {
            log.warning("handleVlmEvalPrompt failed: " + e.getMessage());
        }
    }

    // -- Chat message persistence -----------------------------------------------

    void saveChatMessage(String workflowId, String role, String content) {
        if (content == null || content.isBlank()) return;
        try {
            if (workflowId != null) {
                mgr.jdbc.update(
                        "INSERT INTO chat_messages (conversation_id, workflow_id, role, content) " +
                        "VALUES (:convId::uuid, :wfId::uuid, :role, :content)",
                        new MapSqlParameterSource()
                                .addValue("convId",   conversationId)
                                .addValue("wfId",     workflowId)
                                .addValue("role",     role)
                                .addValue("content",  content));
            } else {
                mgr.jdbc.update(
                        "INSERT INTO chat_messages (conversation_id, role, content) " +
                        "VALUES (:convId::uuid, :role, :content)",
                        Map.of("convId", conversationId, "role", role, "content", content));
            }
        } catch (Exception e) {
            log.warning("saveChatMessage failed: " + e.getMessage());
        }
    }

    // -- Scoring context --------------------------------------------------------

    private String buildScoringMessage(String imageUuid, JsonNode msg, Budget budget) {
        JsonNode scores       = msg.path("scores");
        double clipScore      = scores.path("clip_score").asDouble(0.0);
        double artifactScore  = scores.path("artifact_score").asDouble(1.0);
        JsonNode vlmScores    = scores.path("vlm_scores");
        String prompt         = msg.path("prompt").asText("");
        String verdictType    = msg.path("verdict").asText("candidate");
        String rejectionReason = ConversationAgentManager.nullIfBlank(msg.path("rejection_reason").asText(""));
        String imagePath      = ConversationAgentManager.nullIfBlank(msg.path("image_path").asText(""));
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

    // -- Budget -----------------------------------------------------------------

    private record Budget(int retriesUsed, int inpaintsUsed, int maxRetries, int maxInpaints) {}

    private Budget loadBudget(String sessionUuid) {
        List<Map<String, Object>> rows = mgr.jdbc.queryForList(
                "SELECT retries_used, inpaints_used, max_retries, max_inpaints " +
                "FROM budget WHERE session_uuid = :sess::uuid",
                Map.of("sess", sessionUuid));
        if (rows.isEmpty()) {
            return new Budget(0, 0,
                    mgr.cfg.getDecisions().getMaxRetries(),
                    mgr.cfg.getDecisions().getMaxInpaints());
        }
        Map<String, Object> r = rows.get(0);
        return new Budget(
                ((Number) r.get("retries_used")).intValue(),
                ((Number) r.get("inpaints_used")).intValue(),
                ((Number) r.get("max_retries")).intValue(),
                ((Number) r.get("max_inpaints")).intValue());
    }

    private void incrementBudget(String sessionUuid, String field) {
        mgr.jdbc.update(
                "UPDATE budget SET " + field + " = " + field + " + 1 WHERE session_uuid = :sess::uuid",
                Map.of("sess", sessionUuid));
    }

    // -- Auto-title -------------------------------------------------------------

    private void maybeAutoTitle(List<ObjectNode> messages) {
        List<String> names = mgr.jdbc.queryForList(
                "SELECT name FROM conversations WHERE conversation_id = :id::uuid",
                Map.of("id", conversationId), String.class);
        if (names.isEmpty() || !"Untitled".equals(names.get(0))) return;

        String firstUserMsg = messages.stream()
                .filter(m -> "user".equals(m.path("role").asText("")))
                .map(m -> m.path("content").asText(""))
                .findFirst().orElse("");
        if (firstUserMsg.isBlank()) return;

        Thread.ofVirtual().name("auto-title-" + conversationId.substring(0, 8)).start(() -> {
            try {
                String snippet = firstUserMsg.substring(0, Math.min(200, firstUserMsg.length()));
                ObjectNode body = mgr.mapper.createObjectNode();
                body.put("model", mgr.resolvedModelId);
                body.put("temperature", 0.3);
                body.put("max_tokens", 16);
                body.putArray("messages").add(mgr.mapper.createObjectNode()
                        .put("role", "user")
                        .put("content",
                             "Respond with a short title (2-4 words) capturing the subject, " +
                             "character, or theme of this image request. Do not include words like " +
                             "'image', 'generate', 'picture', 'creation', or any task description. " +
                             "Title case, no punctuation, no quotes:\n" + snippet));
                ObjectNode resp = mgr.llmClient.post()
                        .uri("/chat/completions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(mgr.mapper.writeValueAsBytes(body))
                        .retrieve().body(ObjectNode.class);
                String title = resp.path("choices").path(0).path("message").path("content")
                        .asText("").strip().replaceAll("[\"']", "");
                if (title.isBlank() || title.length() > 80) return;
                mgr.jdbc.update("UPDATE conversations SET name = :name WHERE conversation_id = :id::uuid",
                        Map.of("name", title, "id", conversationId));
                mgr.chatBroadcast.sendRename(conversationId, title);
                log.info("Auto-titled " + conversationId + ": " + title);
            } catch (Exception e) {
                log.warning("Auto-title failed: " + e.getMessage());
            }
        });
    }

    // -- Conversation memory ----------------------------------------------------

    private String loadConversationMemory(String convId) {
        if (convId == null) return null;
        List<String> rows = mgr.jdbc.queryForList(
                "SELECT content FROM conversation_memory WHERE conversation_id = :id::uuid",
                Map.of("id", convId), String.class);
        if (rows.isEmpty()) return null;
        return "=== CONVERSATION MEMORY ==========================================\n" + rows.get(0) + "\n";
    }

    private void maybeExtractMemory() {
        Thread.ofVirtual().name("mem-extract-" + conversationId.substring(0, 8)).start(() -> {
            try {
                List<Map<String, Object>> rows = mgr.jdbc.queryForList(
                        "SELECT role, content FROM (" +
                        "  SELECT role, content, created_at FROM chat_messages " +
                        "  WHERE conversation_id = :id::uuid " +
                        "  ORDER BY created_at DESC LIMIT 10" +
                        ") sub ORDER BY created_at ASC",
                        Map.of("id", conversationId));
                if (rows.size() < 2) return;

                List<String> existing = mgr.jdbc.queryForList(
                        "SELECT content FROM conversation_memory WHERE conversation_id = :id::uuid",
                        Map.of("id", conversationId), String.class);
                String currentMemory = existing.isEmpty() ? null : existing.get(0);

                StringBuilder transcript = new StringBuilder();
                for (Map<String, Object> row : rows) {
                    transcript.append(row.get("role")).append(": ")
                              .append(row.get("content")).append("\n\n");
                }

                String extractPrompt = (currentMemory != null
                        ? "Current memory:\n" + currentMemory + "\n\n"
                        : "") +
                        "Recent conversation:\n" + transcript +
                        "\nUpdate the memory to capture any character names, physical descriptions, " +
                        "style preferences, confirmed decisions, or stated constraints from this conversation. " +
                        "Be concise (under 150 words). Write only the memory text, no preamble. " +
                        "If nothing new, return the existing memory unchanged. /no_think";

                ObjectNode body = mgr.mapper.createObjectNode();
                body.put("model",       mgr.resolvedModelId);
                body.put("temperature", 0.1);
                body.put("max_tokens",  256);
                body.putArray("messages").add(
                        mgr.mapper.createObjectNode().put("role", "user").put("content", extractPrompt));

                ObjectNode resp = mgr.llmClient.post()
                        .uri("/chat/completions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .body(mgr.mapper.writeValueAsBytes(body))
                        .retrieve().body(ObjectNode.class);

                String newMemory = resp.path("choices").path(0).path("message").path("content")
                        .asText("").strip();
                if (newMemory.contains("<think>")) {
                    int end = newMemory.indexOf("</think>");
                    if (end != -1) newMemory = newMemory.substring(end + 8).strip();
                }
                if (newMemory.isBlank()) return;

                final String mem = newMemory;
                mgr.jdbc.update(
                        "INSERT INTO conversation_memory (conversation_id, content) " +
                        "VALUES (:id::uuid, :content) " +
                        "ON CONFLICT (conversation_id) DO UPDATE SET content = :content, last_updated_at = now()",
                        Map.of("id", conversationId, "content", mem));

                log.fine("Conversation memory updated for " + conversationId);
            } catch (Exception e) {
                log.warning("Memory extraction failed for " + conversationId + ": " + e.getMessage());
            }
        });
    }

    // -- Heuristic fallback -----------------------------------------------------

    private JsonNode heuristicDecision(JsonNode msg, Budget budget) {
        JsonNode scores  = msg.path("scores");
        double clipScore = scores.path("clip_score").asDouble(0.0);
        double vlmMean   = computeVlmMean(scores.path("vlm_scores"));

        if (vlmMean >= mgr.cfg.getDecisions().getAcceptVlmMeanMin()
                && clipScore >= mgr.cfg.getDecisions().getAcceptClipMin()) {
            return mgr.mapper.createObjectNode()
                    .put("decision", "accept").put("message", "Accepted. Quality thresholds met.")
                    .put("reasoning", "Heuristic.").put("confidence", 0.7);
        }
        if (budget.retriesUsed() < budget.maxRetries()) {
            return mgr.mapper.createObjectNode()
                    .put("decision", "retry")
                    .put("message", String.format("Retrying (vlm_mean=%.1f, clip=%.3f).", vlmMean, clipScore))
                    .put("reasoning", "Heuristic retry.").put("confidence", 0.5)
                    .put("retry_prompt", msg.path("prompt").asText(""));
        }
        return mgr.mapper.createObjectNode()
                .put("decision", "give_up").put("message", "Budget exhausted.")
                .put("reasoning", "Budget exhausted.").put("confidence", 1.0);
    }

    private double computeVlmMean(JsonNode vlmScores) {
        String[] dims = {"photorealism","anatomical_coherence","interaction_plausibility",
                         "lighting_consistency","prompt_adherence"};
        double sum = 0; int count = 0;
        for (String d : dims) if (vlmScores.has(d)) { sum += vlmScores.get(d).asDouble(); count++; }
        return count > 0 ? sum / count : 0.0;
    }

    private boolean shouldThink(JsonNode msg, Budget budget) {
        if (budget.retriesUsed() >= budget.maxRetries() && budget.inpaintsUsed() >= budget.maxInpaints()) return false;
        double clipScore = msg.path("scores").path("clip_score").asDouble(0.0);
        double vlmMean   = computeVlmMean(msg.path("scores").path("vlm_scores"));
        return vlmMean >= 4.5 && vlmMean < 7.5 && clipScore >= 0.20;
    }

    // -- Helpers ----------------------------------------------------------------

    private List<String> listWorkflowNames() {
        try {
            if (!Files.isDirectory(mgr.workflowsDir)) return List.of();
            try (var stream = Files.list(mgr.workflowsDir)) {
                return stream.filter(p -> p.getFileName().toString().endsWith(".json"))
                        .map(p -> p.getFileName().toString()).sorted().collect(Collectors.toList());
            }
        } catch (Exception e) { return List.of(); }
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
            return mgr.mapper.readTree(raw);
        } catch (Exception e) { return null; }
    }

    private String readSystemPrompt() {
        try {
            return Files.readString(mgr.systemPromptPath);
        } catch (Exception e) {
            return "You are a tactical image generation agent. Output JSON decisions only.";
        }
    }

    private void initiateShutdown() {
        log.info("Escalation - initiating shutdown");
        Thread.ofVirtual().start(() -> {
            try { Thread.sleep(500); } catch (InterruptedException ignored) {}
            SpringApplication.exit(mgr.applicationContext, () -> 0);
        });
    }

    private static boolean isTimeoutException(Exception e) {
        Throwable t = e;
        while (t != null) {
            if (t instanceof java.net.SocketTimeoutException) return true;
            if (t instanceof java.util.concurrent.TimeoutException) return true;
            t = t.getCause();
        }
        return false;
    }

    // -- Model ID resolution (called once by manager at startup) ---------------

    static String resolveModelId(String configured, RestClient llmClient, ObjectMapper mapper) {
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
                Logger.getLogger(ConversationAgent.class.getName()).info("Resolved model ID: " + id);
                return id;
            }
        } catch (Exception e) {
            Logger.getLogger(ConversationAgent.class.getName())
                    .warning("Could not resolve model ID: " + e.getMessage());
        }
        return configured != null ? configured : "default";
    }
}
