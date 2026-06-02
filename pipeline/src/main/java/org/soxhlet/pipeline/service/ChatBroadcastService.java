package org.soxhlet.pipeline.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Service;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.logging.Logger;

@Service
public class ChatBroadcastService {

    private static final Logger log = Logger.getLogger(ChatBroadcastService.class.getName());

    private final ObjectMapper mapper;
    private final Map<String, Set<WebSocketSession>> sessionsByConversation = new ConcurrentHashMap<>();
    private final Map<WebSocketSession, String> conversationBySession = new ConcurrentHashMap<>();

    public ChatBroadcastService(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    public void register(String conversationId, WebSocketSession session) {
        String prev = conversationBySession.put(session, conversationId);
        if (prev != null && !prev.equals(conversationId)) {
            sessionsByConversation.getOrDefault(prev, ConcurrentHashMap.newKeySet()).remove(session);
        }
        sessionsByConversation.computeIfAbsent(conversationId, k -> ConcurrentHashMap.newKeySet()).add(session);
    }

    public void deregister(WebSocketSession session) {
        String convId = conversationBySession.remove(session);
        if (convId != null) {
            Set<WebSocketSession> sessions = sessionsByConversation.get(convId);
            if (sessions != null) sessions.remove(session);
        }
    }

    public void sendMessage(String conversationId, String text) {
        sendChunk(conversationId, text);
        sendDone(conversationId);
    }

    public void sendRename(String conversationId, String name) {
        ObjectNode msg = mapper.createObjectNode();
        msg.put("type", "conv_renamed");
        msg.put("conversation_id", conversationId);
        msg.put("name", name);
        broadcast(conversationId, msg);
    }

    public void sendChunk(String conversationId, String text) {
        ObjectNode msg = mapper.createObjectNode();
        msg.put("type", "chunk");
        msg.put("conversation_id", conversationId);
        msg.put("text", text);
        broadcast(conversationId, msg);
    }

    public void sendDone(String conversationId) {
        ObjectNode msg = mapper.createObjectNode();
        msg.put("type", "done");
        msg.put("conversation_id", conversationId);
        broadcast(conversationId, msg);
    }

    public void sendProgress(String conversationId, String text) {
        ObjectNode msg = mapper.createObjectNode();
        msg.put("type", "progress");
        msg.put("conversation_id", conversationId);
        msg.put("text", text);
        broadcast(conversationId, msg);
    }

    public void sendError(String conversationId, String text) {
        ObjectNode msg = mapper.createObjectNode();
        msg.put("type", "error");
        msg.put("conversation_id", conversationId);
        msg.put("text", text);
        broadcast(conversationId, msg);
    }

    private void broadcast(String conversationId, ObjectNode payload) {
        Set<WebSocketSession> sessions = sessionsByConversation.get(conversationId);
        if (sessions == null || sessions.isEmpty()) return;
        TextMessage message;
        try {
            message = new TextMessage(mapper.writeValueAsString(payload));
        } catch (Exception e) {
            log.warning("Failed to serialize broadcast message: " + e.getMessage());
            return;
        }
        for (WebSocketSession session : sessions) {
            if (!session.isOpen()) {
                sessions.remove(session);
                conversationBySession.remove(session);
                continue;
            }
            try {
                synchronized (session) {
                    session.sendMessage(message);
                }
            } catch (Exception e) {
                sessions.remove(session);
                conversationBySession.remove(session);
            }
        }
    }
}
