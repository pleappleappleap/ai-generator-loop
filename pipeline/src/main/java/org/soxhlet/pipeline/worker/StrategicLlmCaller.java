package org.soxhlet.pipeline.worker;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.soxhlet.pipeline.config.StrategicLlmConfig;
import org.soxhlet.pipeline.service.ContextService;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.logging.Logger;

@Component
public class StrategicLlmCaller {

    public enum State { IDLE, RUNNING, COMPLETE, FAILED }

    public record SessionResult(
            State state,
            String northStar,
            String sessionDirection,
            String analysis,
            String reasoning,
            String error) {}

    private static final Logger log = Logger.getLogger(StrategicLlmCaller.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final ContextService contextService;
    private final RestClient llmClient;
    private final ObjectMapper mapper;
    private final StrategicLlmConfig cfg;
    private final String systemPrompt;

    private final AtomicReference<State> state = new AtomicReference<>(State.IDLE);
    private volatile SessionResult lastResult;

    public StrategicLlmCaller(
            NamedParameterJdbcTemplate jdbc,
            ContextService contextService,
            ObjectMapper mapper,
            StrategicLlmConfig cfg,
            @Qualifier("strategicLlmClient") RestClient llmClient,
            @Value("${strategic-llm.system-prompt-file:strategic-llm/system_prompt.md}") String systemPromptFile) {
        this.jdbc = jdbc;
        this.contextService = contextService;
        this.mapper = mapper;
        this.cfg = cfg;
        this.llmClient = llmClient;
        this.systemPrompt = loadSystemPrompt(systemPromptFile);
    }

    // ── Auto-detection ─────────────────────────────────────────────────────────

    @Scheduled(fixedDelay = 10000)
    public void checkPendingEscalation() {
        if (state.get() == State.RUNNING) return;

        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT id, reason FROM pipeline_events " +
                "WHERE type = 'mode_switch_requested' AND handled_at IS NULL " +
                "ORDER BY created_at LIMIT 1",
                Map.of());

        if (rows.isEmpty()) return;

        Map<String, Object> row = rows.get(0);
        Long eventId = ((Number) row.get("id")).longValue();
        String reason = row.get("reason") != null ? row.get("reason").toString() : "";

        log.info("Pending escalation (event=" + eventId + ") — starting strategic session");
        runSession(eventId, reason);
    }

    // ── Session execution ──────────────────────────────────────────────────────

    public SessionResult runManualSession() {
        // Reset to IDLE so we can transition to RUNNING
        state.set(State.IDLE);
        return runSession(null, "Manual operator-triggered strategic review");
    }

    public SessionResult runSession(Long eventId, String escalationReason) {
        if (!state.compareAndSet(State.IDLE, State.RUNNING)
                && !state.compareAndSet(State.COMPLETE, State.RUNNING)
                && !state.compareAndSet(State.FAILED, State.RUNNING)) {
            return lastResult;
        }

        try {
            String prompt = buildStrategicPrompt(escalationReason);
            JsonNode parsed = callStrategicLlm(prompt);

            if (parsed == null) {
                throw new RuntimeException("Strategic LLM returned no parseable response");
            }

            String newNorthStar       = parsed.path("north_star").asText("");
            String newSessionDir      = parsed.path("session_direction").isNull() ? null
                                            : parsed.path("session_direction").asText(null);
            String analysis           = parsed.path("analysis").asText("");
            String reasoning          = parsed.path("reasoning").asText("");

            if (!newNorthStar.isBlank()) {
                contextService.writeNorthStar(newNorthStar, "strategic_llm");
                log.info("North star updated by strategic LLM");
            }

            if (newSessionDir != null && !newSessionDir.isBlank()) {
                jdbc.update(
                        "UPDATE session_direction SET superseded_at = now() WHERE superseded_at IS NULL",
                        Map.of());
                jdbc.update(
                        "INSERT INTO session_direction (content, created_by) VALUES (:content, 'strategic_llm')",
                        Map.of("content", newSessionDir));
                log.info("Session direction updated by strategic LLM");
            }

            if (eventId != null) {
                jdbc.update(
                        "UPDATE pipeline_events SET handled_at = now() WHERE id = :id",
                        Map.of("id", eventId));
            }

            jdbc.update(
                    "INSERT INTO pipeline_events (type, reason) VALUES ('mode_switch_complete', :reason)",
                    Map.of("reason", "Strategic session completed. " + reasoning));

            SessionResult result = new SessionResult(
                    State.COMPLETE, newNorthStar, newSessionDir, analysis, reasoning, null);
            lastResult = result;
            state.set(State.COMPLETE);
            log.info("Strategic session complete");
            return result;

        } catch (Exception e) {
            log.warning("Strategic session failed: " + e.getMessage());
            SessionResult result = new SessionResult(
                    State.FAILED, null, null, null, null, e.getMessage());
            lastResult = result;
            state.set(State.FAILED);
            return result;
        }
    }

    public State getState() { return state.get(); }

    public SessionResult getLastResult() { return lastResult; }

    // ── LLM call ───────────────────────────────────────────────────────────────

    private JsonNode callStrategicLlm(String prompt) {
        ObjectNode body = mapper.createObjectNode();
        body.put("model", cfg.getModel());
        body.put("temperature", 0.6);

        ArrayNode messages = body.putArray("messages");
        messages.addObject().put("role", "system").put("content", systemPrompt);
        messages.addObject().put("role", "user").put("content", prompt);

        ObjectNode response;
        try {
            response = llmClient.post()
                    .uri("/chat/completions")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .retrieve()
                    .body(ObjectNode.class);
        } catch (Exception e) {
            log.warning("Strategic LLM call failed: " + e.getMessage());
            return null;
        }

        String raw = response.path("choices").path(0).path("message").path("content").asText("").strip();
        return parseResponseJson(raw);
    }

    private JsonNode parseResponseJson(String raw) {
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
            log.warning("Failed to parse strategic LLM response JSON: " + e.getMessage());
            return null;
        }
    }

    // ── Prompt builder ─────────────────────────────────────────────────────────

    private String buildStrategicPrompt(String escalationReason) {
        StringBuilder sb = new StringBuilder();

        sb.append("── ESCALATION REASON ────────────────────────────────────────\n");
        sb.append("  ").append(escalationReason.isBlank() ? "(none provided)" : escalationReason).append("\n\n");

        String northStar = contextService.northStar();
        if (northStar != null) {
            sb.append("── CURRENT NORTH STAR ───────────────────────────────────────\n");
            sb.append(northStar).append("\n\n");
        } else {
            sb.append("── CURRENT NORTH STAR ───────────────────────────────────────\n");
            sb.append("  (none set)\n\n");
        }

        String sessionDir = jdbc.queryForList(
                "SELECT content FROM session_direction WHERE superseded_at IS NULL " +
                "ORDER BY created_at DESC LIMIT 1",
                Map.of(), String.class).stream().findFirst().orElse(null);
        if (sessionDir != null) {
            sb.append("── SESSION DIRECTION ────────────────────────────────────────\n");
            sb.append(sessionDir).append("\n\n");
        }

        sb.append("── RECENT IMAGES (last 20) ──────────────────────────────────\n");
        List<Map<String, Object>> images = jdbc.queryForList(
                "SELECT sequence_number, prompt, clip_score, artifact_score, " +
                "vlm_scores::text, decision, created_at " +
                "FROM images ORDER BY created_at DESC LIMIT 20",
                Map.of());
        if (images.isEmpty()) {
            sb.append("  (none)\n");
        } else {
            for (Map<String, Object> img : images) {
                sb.append(String.format("  seq=%-3s decision=%-8s clip=%.3f%n",
                        img.get("sequence_number"),
                        img.get("decision") != null ? img.get("decision") : "pending",
                        img.get("clip_score") != null ? ((Number) img.get("clip_score")).doubleValue() : 0.0));
                if (img.get("prompt") != null) {
                    String p = img.get("prompt").toString();
                    sb.append("    prompt: ").append(p, 0, Math.min(120, p.length())).append("\n");
                }
            }
        }
        sb.append("\n");

        sb.append("── USER FEEDBACK (last 20) ──────────────────────────────────\n");
        List<Map<String, Object>> feedback = jdbc.queryForList(
                "SELECT uf.rating, uf.comment, i.prompt " +
                "FROM user_feedback uf LEFT JOIN images i USING (image_uuid) " +
                "ORDER BY uf.created_at DESC LIMIT 20",
                Map.of());
        if (feedback.isEmpty()) {
            sb.append("  (none)\n");
        } else {
            for (Map<String, Object> fb : feedback) {
                sb.append(String.format("  %s%s%n",
                        fb.get("rating"),
                        fb.get("comment") != null ? " — " + fb.get("comment") : ""));
                if (fb.get("prompt") != null) {
                    String p = fb.get("prompt").toString();
                    sb.append("    prompt: ").append(p, 0, Math.min(80, p.length())).append("\n");
                }
            }
        }
        sb.append("\n");

        List<String> taste = jdbc.queryForList(
                "SELECT content FROM taste_synthesis ORDER BY created_at DESC LIMIT 1",
                Map.of(), String.class);
        if (!taste.isEmpty()) {
            sb.append("── TASTE SYNTHESIS ──────────────────────────────────────────\n");
            sb.append(taste.get(0)).append("\n\n");
        }

        return sb.toString();
    }

    // ── System prompt ──────────────────────────────────────────────────────────

    private String loadSystemPrompt(String filePath) {
        try {
            return Files.readString(Path.of(filePath));
        } catch (Exception e) {
            log.warning("Cannot load strategic LLM system prompt from '" + filePath + "': " + e.getMessage());
            return DEFAULT_SYSTEM_PROMPT;
        }
    }

    private static final String DEFAULT_SYSTEM_PROMPT =
            "You are the strategic director of an AI image generation system. " +
            "Review the session context and return a JSON object with keys: " +
            "analysis, north_star, session_direction (or null), reasoning.";
}
