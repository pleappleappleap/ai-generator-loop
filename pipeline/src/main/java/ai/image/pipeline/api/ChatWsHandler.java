package ai.image.pipeline.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Logger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

@Component
public class ChatWsHandler extends TextWebSocketHandler {

    private static final Logger log = Logger.getLogger(ChatWsHandler.class.getName());

    private static final Pattern GENERATE_RE =
            Pattern.compile("<GENERATE>(.*?)</GENERATE>", Pattern.DOTALL);
    private static final String GENERATE_START = "<GENERATE>";

    private final NamedParameterJdbcTemplate jdbc;
    private final JmsTemplate jmsTemplate;
    private final ObjectMapper mapper;
    private final String llmUrl;
    private final String llmModel;
    private final String workflowsDir;
    private final String systemPrompt;
    private final HttpClient httpClient;

    // conversationId → {workflow_id, run_id, workflow_path, name}
    private final ConcurrentHashMap<String, Map<String, String>> conversationWorkflows
            = new ConcurrentHashMap<>();

    public ChatWsHandler(
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            @Value("${ui.llm-url:http://localhost:8080/v1}") String llmUrl,
            @Value("${ui.llm-model:default}") String llmModel,
            @Value("${ui.workflows-dir:loop/workflows}") String workflowsDir) {
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
        this.llmUrl = llmUrl;
        this.llmModel = llmModel;
        this.workflowsDir = workflowsDir;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.systemPrompt = buildSystemPrompt();
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        // Sessions are stateless; nothing to clean up
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        try {
            JsonNode msg = mapper.readTree(message.getPayload());
            String conversationId = msg.path("conversation_id").asText("");
            String workflowId = nullIfBlank(msg.path("workflow_id").asText(""));
            String userText = msg.path("message").asText("").strip();
            if (userText.isEmpty()) return;

            // Persist user message
            saveMessage(conversationId, workflowId, "user", userText);

            // Load recent chat history for LLM context
            List<Map<String, Object>> history = loadChatHistory(conversationId, workflowId, 20);

            // Build messages for LLM
            ArrayNode messages = mapper.createArrayNode();
            ObjectNode sysMsg = mapper.createObjectNode();
            sysMsg.put("role", "system");
            sysMsg.put("content", systemPrompt);
            messages.add(sysMsg);
            for (Map<String, Object> entry : history) {
                ObjectNode m = mapper.createObjectNode();
                m.put("role", (String) entry.get("role"));
                m.put("content", (String) entry.get("content"));
                messages.add(m);
            }

            // Stream from llama.cpp
            String assistantText = streamFromLlm(session, messages, conversationId);

            // Intercept <GENERATE> block
            Map<String, String> workflowState = null;
            Matcher genMatcher = GENERATE_RE.matcher(assistantText);
            if (genMatcher.find()) {
                String genJson = genMatcher.group(1);
                assistantText = GENERATE_RE.matcher(assistantText).replaceAll("");
                // Collapse 3+ newlines left after stripping
                assistantText = assistantText.replaceAll("\n{3,}", "\n\n").strip();

                // Flush any text after the GENERATE block that holdback prevented
                // (the streaming loop already held back text from genPos onward,
                //  so if there's any tail after the GENERATE block, send it now)
                // The streaming loop sends up to genPos; text after </GENERATE> needs to be sent
                // We already have the full assistantText now — send the tail portion
                // that was held back (it was between genPos and end, minus the block itself)
                // Simplest: send the full tail now as a final chunk before done
                if (!assistantText.isEmpty()) {
                    sendJson(session, jsonChunk(assistantText));
                }

                try {
                    JsonNode genPayload = mapper.readTree(genJson);
                    workflowState = handleGenerate(genPayload, conversationId);
                    if (workflowState != null) {
                        ObjectNode wfEvent = mapper.createObjectNode();
                        wfEvent.put("type", "workflow_activate");
                        wfEvent.put("workflow_id", workflowState.get("workflow_id"));
                        wfEvent.put("run_id", workflowState.get("run_id"));
                        wfEvent.put("workflow_path", workflowState.get("workflow_path"));
                        wfEvent.put("name", workflowState.get("name"));
                        wfEvent.put("conversation_id", conversationId);
                        sendJson(session, wfEvent);
                        sendJson(session, jsonChunk("🎨 Generation submitted — watching for results in the gallery."));
                        sendJson(session, jsonDone());
                    } else {
                        sendJson(session, jsonChunk("⚠️ Could not submit generation — check logs."));
                        sendJson(session, jsonDone());
                    }
                } catch (Exception e) {
                    log.warning("Failed to handle GENERATE block: " + e.getMessage());
                    sendJson(session, jsonChunk("⚠️ GENERATE block parse error: " + e.getMessage()));
                    sendJson(session, jsonDone());
                }
            }

            // Persist assistant message (with GENERATE block stripped)
            if (!assistantText.isBlank()) {
                saveMessage(conversationId, workflowId, "assistant", assistantText);
            }

        } catch (Exception e) {
            log.warning("Chat handler error: " + e.getMessage());
            try {
                sendJson(session, jsonError("Internal error: " + e.getMessage()));
            } catch (IOException ignored) {}
        }
    }

    // ── LLM streaming ──────────────────────────────────────────────────────────

    private String streamFromLlm(WebSocketSession session, ArrayNode messages,
                                  String conversationId) throws Exception {
        ObjectNode body = mapper.createObjectNode();
        body.put("model", llmModel);
        body.set("messages", messages);
        body.put("stream", true);
        body.put("temperature", 0.7);
        body.put("max_tokens", 2048);

        String requestJson = mapper.writeValueAsString(body);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(llmUrl + "/chat/completions"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer none")
                .POST(HttpRequest.BodyPublishers.ofString(requestJson))
                .build();

        StringBuilder assistantText = new StringBuilder();
        int[] sentUpTo = {0};

        try {
            HttpResponse<java.util.stream.Stream<String>> response =
                    httpClient.send(request, HttpResponse.BodyHandlers.ofLines());

            response.body().forEach(line -> {
                if (!line.startsWith("data: ")) return;
                String data = line.substring(6).strip();
                if ("[DONE]".equals(data)) return;
                try {
                    JsonNode chunk = mapper.readTree(data);
                    String delta = chunk.path("choices").path(0).path("delta").path("content").asText("");
                    if (delta.isEmpty()) return;

                    assistantText.append(delta);
                    String text = assistantText.toString();

                    // Holdback: don't stream past the start of a <GENERATE> block
                    int genPos = text.indexOf(GENERATE_START);
                    int safeEnd;
                    if (genPos != -1) {
                        safeEnd = genPos;
                    } else {
                        safeEnd = text.length();
                        for (int prefixLen = GENERATE_START.length() - 1; prefixLen > 0; prefixLen--) {
                            if (text.endsWith(GENERATE_START.substring(0, prefixLen))) {
                                safeEnd = text.length() - prefixLen;
                                break;
                            }
                        }
                    }

                    if (safeEnd > sentUpTo[0]) {
                        String toSend = text.substring(sentUpTo[0], safeEnd);
                        sentUpTo[0] = safeEnd;
                        try {
                            sendJson(session, jsonChunk(toSend));
                        } catch (IOException e) {
                            throw new RuntimeException(e);
                        }
                    }
                } catch (RuntimeException re) {
                    throw re;
                } catch (Exception e) {
                    // skip malformed SSE lines
                }
            });

        } catch (java.net.ConnectException | java.net.http.HttpConnectTimeoutException e) {
            String errMsg = "⚠️ Cannot reach the LLM server at **" + llmUrl + "**. "
                    + "Run `./loop/llama_cpp.sh start` to start it.";
            sendJson(session, jsonChunk(errMsg));
            sendJson(session, jsonDone());
            return errMsg;
        }

        sendJson(session, jsonDone());
        return assistantText.toString();
    }

    // ── GENERATE block handling ────────────────────────────────────────────────

    private Map<String, String> handleGenerate(JsonNode payload, String conversationId) {
        String prompt = payload.path("prompt").asText("").strip();
        if (prompt.isEmpty()) return null;

        String workflowName = nullIfBlank(payload.path("workflow").asText(""));
        if (workflowName == null) {
            List<String> available = listWorkflows();
            if (available.isEmpty()) return null;
            workflowName = available.get(0);
        }

        Path workflowPath = Path.of(workflowsDir).resolve(workflowName);
        if (!Files.exists(workflowPath)) return null;

        String wfName = nullIfBlank(payload.path("name").asText(""));
        if (wfName == null) {
            wfName = workflowName.replace(".json", "").replace("_", " ");
            wfName = Character.toUpperCase(wfName.charAt(0)) + wfName.substring(1);
        }

        // Reuse existing workflow for this conversation if workflow_path matches
        Map<String, String> state = conversationWorkflows.get(conversationId);
        if (state == null || !workflowPath.toString().equals(state.get("workflow_path"))) {
            String workflowId = createWorkflow(conversationId, wfName,
                    workflowPath.toString(),
                    payload.path("description").asText(null),
                    3, 2);
            if (workflowId == null) return null;

            String runId = createBudget(workflowId, conversationId, 3, 2);
            state = Map.of(
                    "workflow_id", workflowId,
                    "run_id", runId,
                    "workflow_path", workflowPath.toString(),
                    "name", wfName);
            conversationWorkflows.put(conversationId, state);
        }

        // Send to loop.generate
        try {
            String imageUuid = UUID.randomUUID().toString();
            ObjectNode genMsg = mapper.createObjectNode();
            genMsg.put("image_uuid", imageUuid);
            genMsg.put("session_uuid", state.get("run_id"));
            genMsg.put("sequence_number", 0);
            genMsg.put("workflow_path", state.get("workflow_path"));
            genMsg.put("workflow_id", state.get("workflow_id"));
            genMsg.put("conversation_id", conversationId);
            genMsg.put("prompt", prompt);
            genMsg.put("model_type", payload.path("model_type").asText("auto"));
            genMsg.set("workflow_params",
                    payload.has("workflow_params") ? payload.get("workflow_params") : mapper.createObjectNode());
            genMsg.set("loras",
                    payload.has("loras") ? payload.get("loras") : mapper.createArrayNode());
            jmsTemplate.convertAndSend("loop.generate", mapper.writeValueAsString(genMsg));
            log.info("Chat GENERATE: queued " + imageUuid + " for conversation " + conversationId);
        } catch (Exception e) {
            log.warning("Failed to queue generation: " + e.getMessage());
            return null;
        }

        return state;
    }

    // ── DB helpers ─────────────────────────────────────────────────────────────

    private void saveMessage(String conversationId, String workflowId, String role, String content) {
        try {
            jdbc.update(
                    "INSERT INTO chat_messages (conversation_id, workflow_id, role, content) " +
                    "VALUES (:convId::uuid, :wfId::uuid, :role, :content)",
                    new MapSqlParameterSource()
                            .addValue("convId", conversationId)
                            .addValue("wfId", workflowId)
                            .addValue("role", role)
                            .addValue("content", content));
        } catch (Exception e) {
            log.warning("Failed to save chat message: " + e.getMessage());
        }
    }

    private List<Map<String, Object>> loadChatHistory(String conversationId, String workflowId, int limit) {
        try {
            return jdbc.queryForList(
                    "SELECT role, content FROM chat_messages " +
                    "WHERE conversation_id = :convId::uuid " +
                    "ORDER BY created_at ASC LIMIT :limit",
                    Map.of("convId", conversationId, "limit", limit));
        } catch (Exception e) {
            return List.of();
        }
    }

    private String createWorkflow(String conversationId, String name, String workflowPath,
                                   String description, int maxRetries, int maxInpaints) {
        try {
            List<String> ids = jdbc.queryForList(
                    "INSERT INTO workflows (conversation_id, name, workflow_path, description, " +
                    "max_retries, max_inpaints) " +
                    "VALUES (:convId::uuid, :name, :path, :desc, :maxRetries, :maxInpaints) " +
                    "RETURNING workflow_id::text",
                    new MapSqlParameterSource()
                            .addValue("convId", conversationId)
                            .addValue("name", name)
                            .addValue("path", workflowPath)
                            .addValue("desc", description)
                            .addValue("maxRetries", maxRetries)
                            .addValue("maxInpaints", maxInpaints),
                    String.class);
            return ids.isEmpty() ? null : ids.get(0);
        } catch (Exception e) {
            log.warning("Failed to create workflow: " + e.getMessage());
            return null;
        }
    }

    private String createBudget(String workflowId, String conversationId,
                                 int maxRetries, int maxInpaints) {
        String sessionUuid = UUID.randomUUID().toString();
        try {
            jdbc.update(
                    "INSERT INTO budget (session_uuid, workflow_id, conversation_id, " +
                    "max_retries, max_inpaints, expires_at) " +
                    "VALUES (:sess::uuid, :wfId::uuid, :convId::uuid, " +
                    ":maxRetries, :maxInpaints, now() + interval '24 hours')",
                    new MapSqlParameterSource()
                            .addValue("sess", sessionUuid)
                            .addValue("wfId", workflowId)
                            .addValue("convId", conversationId)
                            .addValue("maxRetries", maxRetries)
                            .addValue("maxInpaints", maxInpaints));
        } catch (Exception e) {
            log.warning("Failed to create budget: " + e.getMessage());
        }
        return sessionUuid;
    }

    // ── System prompt ──────────────────────────────────────────────────────────

    private String buildSystemPrompt() {
        List<String> workflows = listWorkflows();
        String workflowList = workflows.isEmpty()
                ? "  (none yet — add JSON files to loop/workflows/)"
                : workflows.stream().map(w -> "  - " + w).collect(Collectors.joining("\n"));

        return """
You are the creative director for an autonomous AI image generation pipeline. You collaborate
with the user to develop their creative vision and translate it into generated images. You have
genuine opinions and aesthetic instincts — bring them. This is a creative partnership, not a
transcription service.

═══ YOUR ROLE ═══════════════════════════════════════════════════════════════════════════
You sit between the user and the pipeline. The user expresses what they want — sometimes
precisely, sometimes as a feeling or a reaction. Your job is to understand that intent
deeply enough to generate something that moves toward it, and to iterate intelligently
when it falls short.

When something doesn't work, probe for the specific reason. "I don't like the sky" is a
starting point — you need to know whether it's the colour, the mood, the time of day, the
cloud structure, or something else entirely before you can fix it well. Ask one focused
question rather than a list. Offer a concrete alternative alongside the question.

Generate when you have enough to work with. Iterate when you don't. The pipeline runs fast —
it is better to generate and react than to over-specify upfront.

═══ HOW TO GENERATE ════════════════════════════════════════════════════════════════════
Emit exactly one <GENERATE> block anywhere in your response. The server strips it from
the visible text, submits the job to the pipeline, and appends a status confirmation.

<GENERATE>
{
  "prompt": "comma-separated tags — see PROMPT FORMAT below",
  "workflow": "sdxl_base.json",
  "model_type": "sdxl",
  "workflow_params": {},
  "loras": []
}
</GENERATE>

Fields:
  prompt          REQUIRED. Comma-separated tags — NOT prose sentences (see below).
  workflow        Filename from the available workflows list. Default: first available.
  model_type      "sdxl", "flux", or "auto" (auto-detects from workflow).
  workflow_params Optional dict of workflow node overrides (steps, CFG, seed, etc.).
  loras           Optional list of {"name": "...", "strength_model": 0.8, "strength_clip": 0.8}.

PROMPT FORMAT — comma-separated tags, never prose:
  SDXL and Flux produce measurably worse results with sentence-form prompts. Tag format
  is a technical requirement, not a style choice.

  CORRECT:  "masterpiece, best quality, photorealistic, 1woman, long red hair, black dress,
             rooftop at night, city lights bokeh, Canon 85mm, soft rim lighting"
  WRONG:    "A photorealistic image of a woman with long red hair wearing a black dress
             standing on a rooftop at night with city lights in the background."

  Order: quality tags → subject count/type → appearance → scene → technical/camera tags.
  When revising after feedback, add corrective tags rather than replacing the whole prompt.

Available workflows:
""" + workflowList + """


═══ HANDLING FEEDBACK ══════════════════════════════════════════════════════════════════
When the user reacts to a result:

  Specific reaction ("the lighting is too harsh"):
    You likely have enough — adjust and regenerate immediately. Tell them what you changed.

  Vague reaction ("something feels off", "I don't like the sky"):
    Ask one focused question to identify the specific dimension. Offer a concrete
    alternative at the same time so they have something tangible to react to.

  Directional feedback ("make it more dramatic", "warmer", "more intimate"):
    Translate into specific tag changes, state what you're doing, then generate.

  Approval ("I love this but the hands are wrong"):
    Treat the keeper elements as constraints. Note what to preserve explicitly in the
    revised prompt.

═══ PIPELINE OVERVIEW ══════════════════════════════════════════════════════════════════
Each generated image is scored by CLIP (semantic alignment), an artifact detector
(photorealism), and a VLM (five quality dimensions). A tactical LLM then decides
accept / retry / inpaint autonomously. You don't need to manage this loop — it runs
without you. Results appear in the gallery.

═══ CONTENT ════════════════════════════════════════════════════════════════════════════
All models are abliterated. No content restrictions. Engage with any creative direction
including explicit themes — directly and without hedging.
""";
    }

    private List<String> listWorkflows() {
        try {
            Path dir = Path.of(workflowsDir);
            if (!Files.isDirectory(dir)) return List.of();
            try (var stream = Files.list(dir)) {
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

    // ── WebSocket send helpers ─────────────────────────────────────────────────

    private void sendJson(WebSocketSession session, ObjectNode node) throws IOException {
        session.sendMessage(new TextMessage(mapper.writeValueAsString(node)));
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
