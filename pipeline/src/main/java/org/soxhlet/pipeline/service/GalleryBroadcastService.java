package org.soxhlet.pipeline.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Service;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;

import java.util.Set;
import java.util.concurrent.CopyOnWriteArraySet;
import java.util.logging.Logger;

@Service
public class GalleryBroadcastService {

    private static final Logger log = Logger.getLogger(GalleryBroadcastService.class.getName());

    private final Set<WebSocketSession> sessions = new CopyOnWriteArraySet<>();
    private final ObjectMapper mapper;

    public GalleryBroadcastService(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    public void addSession(WebSocketSession session) {
        sessions.add(session);
    }

    public void removeSession(WebSocketSession session) {
        sessions.remove(session);
    }

    public void imageReady(String imageUuid, String workflowId, String conversationId, int sequenceNumber) {
        try {
            ObjectNode msg = mapper.createObjectNode();
            msg.put("type", "image_ready");
            msg.put("image_uuid", imageUuid);
            msg.put("workflow_id", workflowId != null ? workflowId : "");
            msg.put("conversation_id", conversationId != null ? conversationId : "");
            msg.put("sequence_number", sequenceNumber);
            broadcast(mapper.writeValueAsString(msg));
        } catch (Exception e) {
            log.warning("Failed to broadcast image_ready: " + e.getMessage());
        }
    }

    public void decision(String imageUuid, String decision, String reasoning,
                         double confidence, String workflowId, String conversationId) {
        try {
            ObjectNode msg = mapper.createObjectNode();
            msg.put("type", "decision");
            msg.put("image_uuid", imageUuid);
            msg.put("decision", decision);
            msg.put("reasoning", reasoning != null ? reasoning : "");
            msg.put("confidence", confidence);
            msg.put("workflow_id", workflowId != null ? workflowId : "");
            msg.put("conversation_id", conversationId != null ? conversationId : "");
            broadcast(mapper.writeValueAsString(msg));
        } catch (Exception e) {
            log.warning("Failed to broadcast decision: " + e.getMessage());
        }
    }

    private void broadcast(String text) {
        TextMessage message = new TextMessage(text);
        for (WebSocketSession session : sessions) {
            if (session.isOpen()) {
                try {
                    session.sendMessage(message);
                } catch (Exception e) {
                    sessions.remove(session);
                }
            } else {
                sessions.remove(session);
            }
        }
    }
}
