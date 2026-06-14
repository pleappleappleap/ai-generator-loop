package org.soxhlet.pipeline.worker;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.soxhlet.pipeline.config.TacticalLlmConfig;
import org.soxhlet.pipeline.service.ChatBroadcastService;
import org.soxhlet.pipeline.service.ContextService;
import org.soxhlet.pipeline.service.GalleryBroadcastService;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ApplicationContext;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.web.client.RestClient;

import java.nio.file.Path;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Logger;

@Service
public class ConversationAgentManager {

    private static final Logger log = Logger.getLogger(ConversationAgentManager.class.getName());

    private final ConcurrentHashMap<String, ConversationAgent> agents = new ConcurrentHashMap<>();

    // Dependencies forwarded to each ConversationAgent
    final PlatformTransactionManager txManager;
    final NamedParameterJdbcTemplate jdbc;
    final JmsTemplate jmsTemplate;
    final ObjectMapper mapper;
    final TacticalLlmConfig cfg;
    final ContextService contextService;
    final RestClient llmClient;
    final RestClient vlmClient;
    final RestClient comfyuiClient;
    final RestClient searxngClient;
    final GalleryBroadcastService galleryBroadcast;
    final ChatBroadcastService chatBroadcast;
    final ApplicationContext applicationContext;
    final Path systemPromptPath;
    final Path workflowsDir;
    final Path comfyuiModelsDir;
    final String resolvedModelId;
    final String llmRestartScript;

    public ConversationAgentManager(
            PlatformTransactionManager txManager,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            TacticalLlmConfig cfg,
            ContextService contextService,
            GalleryBroadcastService galleryBroadcast,
            ChatBroadcastService chatBroadcast,
            ApplicationContext applicationContext,
            @Qualifier("tacticalLlmClient")  RestClient llmClient,
            @Qualifier("vlmScorerClient")    RestClient vlmClient,
            @Qualifier("comfyuiClient")      RestClient comfyuiClient,
            @Qualifier("searxngClient")      RestClient searxngClient,
            @Value("${tactical-llm.restart-script:loop/tactical_llm.sh}") String llmRestartScript,
            @Value("${tactical-llm.system-prompt-file:loop/tactical-llm/system_prompt.md}") String systemPromptFile,
            @Value("${ui.workflows-dir:loop/workflows}")           String workflowsDirStr,
            @Value("${comfyui.models-dir:loop/ComfyUI/models}")    String comfyuiModelsDirStr) {

        this.txManager        = txManager;
        this.jdbc             = jdbc;
        this.jmsTemplate      = jmsTemplate;
        this.mapper           = mapper;
        this.cfg              = cfg;
        this.contextService   = contextService;
        this.galleryBroadcast = galleryBroadcast;
        this.chatBroadcast    = chatBroadcast;
        this.applicationContext = applicationContext;
        this.llmRestartScript = llmRestartScript;
        this.llmClient        = llmClient;
        this.vlmClient        = vlmClient;
        this.comfyuiClient    = comfyuiClient;
        this.searxngClient    = searxngClient;
        this.systemPromptPath = Path.of(systemPromptFile).toAbsolutePath().normalize();
        this.workflowsDir     = Path.of(workflowsDirStr).toAbsolutePath().normalize();
        this.comfyuiModelsDir = Path.of(comfyuiModelsDirStr).toAbsolutePath().normalize();
        this.resolvedModelId  = ConversationAgent.resolveModelId(cfg.getModel(), llmClient, mapper);

        log.info("ConversationAgentManager ready - model: " + this.resolvedModelId);
    }

    /** Route any event to the persistent agent for this conversation. */
    public void dispatch(String conversationId, AgentEvent event) {
        agents.computeIfAbsent(conversationId, this::createAgent).enqueue(event);
    }

    /** Shut down the agent for a conversation (e.g. after archive). */
    public void shutdown(String conversationId) {
        ConversationAgent agent = agents.remove(conversationId);
        if (agent != null) agent.stop();
    }

    @JmsListener(destination = "loop.verdicts")
    public void onVerdict(String body) {
        try {
            JsonNode msg   = mapper.readTree(body);
            String imgUuid = msg.path("image_uuid").asText("");
            if (imgUuid.isBlank()) return;
            // Persist for crash recovery only - agent polls via wait_for_result
            String convId = nullIfBlank(msg.path("conversation_id").asText(""));
            if (convId == null) return;
            jdbc.update(
                "INSERT INTO pending_decisions (image_uuid, session_uuid, verdict_payload) " +
                "VALUES (:id::uuid, :sess::uuid, :payload::jsonb) ON CONFLICT DO NOTHING",
                java.util.Map.of(
                    "id",      imgUuid,
                    "sess",    msg.path("session_uuid").asText(""),
                    "payload", body));
            // NOTE: Do NOT dispatch to agent - agent is polling via wait_for_result tool
        } catch (Exception e) {
            log.warning("onVerdict error: " + e.getMessage());
        }
    }

    void scheduleLlmRestart() {
        Thread.ofVirtual().name("llm-watchdog").start(() -> {
            log.info("LLM response timeout - restarting tactical LLM...");
            try {
                int rc = new ProcessBuilder(llmRestartScript, "restart")
                        .inheritIO()
                        .start()
                        .waitFor();
                log.info("Tactical LLM restart completed (exit " + rc + ")");
            } catch (Exception e) {
                log.warning("Tactical LLM restart failed: " + e.getMessage());
            }
        });
    }

    // -- Internal ---------------------------------------------------------------

    private ConversationAgent createAgent(String conversationId) {
        ConversationAgent agent = new ConversationAgent(conversationId, this);
        Thread.ofVirtual()
              .name("agent-" + conversationId.substring(0, 8))
              .start(agent);
        log.info("Started agent for conversation " + conversationId);
        return agent;
    }

    static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }
}
