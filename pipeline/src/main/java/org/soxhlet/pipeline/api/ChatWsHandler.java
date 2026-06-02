package org.soxhlet.pipeline.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.soxhlet.pipeline.service.ChatBroadcastService;
import org.soxhlet.pipeline.worker.AgentEvent;
import org.soxhlet.pipeline.worker.ConversationAgentManager;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.util.Map;
import java.util.logging.Logger;

@Component
public class ChatWsHandler extends TextWebSocketHandler {

    private static final Logger log = Logger.getLogger(ChatWsHandler.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ConversationAgentManager agentManager;
    private final ChatBroadcastService chatBroadcast;

    public ChatWsHandler(
            NamedParameterJdbcTemplate jdbc,
            ObjectMapper mapper,
            ConversationAgentManager agentManager,
            ChatBroadcastService chatBroadcast) {
        this.jdbc         = jdbc;
        this.mapper       = mapper;
        this.agentManager = agentManager;
        this.chatBroadcast = chatBroadcast;
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        chatBroadcast.deregister(session);
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        try {
            JsonNode msg          = mapper.readTree(message.getPayload());
            String conversationId = msg.path("conversation_id").asText("").strip();
            if (conversationId.isEmpty()) return;

            String type = msg.path("type").asText("chat");

            if ("subscribe".equals(type)) {
                chatBroadcast.register(conversationId, session);
                return;
            }

            String userText = msg.path("message").asText("").strip();
            if (userText.isEmpty()) return;

            chatBroadcast.register(conversationId, session);
            saveUserMessage(conversationId, userText);
            agentManager.dispatch(conversationId, new AgentEvent.UserMessage(userText));

        } catch (Exception e) {
            log.warning("Chat handler error: " + e.getMessage());
        }
    }

    private void saveUserMessage(String conversationId, String content) {
        try {
            jdbc.update(
                    "INSERT INTO chat_messages (conversation_id, role, content) " +
                    "VALUES (:convId::uuid, 'user', :content)",
                    Map.of("convId", conversationId, "content", content));
        } catch (Exception e) {
            log.warning("Failed to save user message: " + e.getMessage());
        }
    }
}
