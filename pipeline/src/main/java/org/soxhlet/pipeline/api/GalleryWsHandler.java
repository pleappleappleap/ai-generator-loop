package org.soxhlet.pipeline.api;

import org.soxhlet.pipeline.service.GalleryBroadcastService;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

@Component
public class GalleryWsHandler extends TextWebSocketHandler {

    private final GalleryBroadcastService broadcaster;

    public GalleryWsHandler(GalleryBroadcastService broadcaster) {
        this.broadcaster = broadcaster;
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        broadcaster.addSession(session);
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        broadcaster.removeSession(session);
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        // Client sends keep-alive pings; nothing to do
    }
}
