package org.soxhlet.pipeline.api;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;

@Configuration
@EnableWebSocket
public class WebSocketConfig implements WebSocketConfigurer {

    private final GalleryWsHandler galleryWsHandler;
    private final ChatWsHandler chatWsHandler;

    public WebSocketConfig(GalleryWsHandler galleryWsHandler, ChatWsHandler chatWsHandler) {
        this.galleryWsHandler = galleryWsHandler;
        this.chatWsHandler = chatWsHandler;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        registry.addHandler(galleryWsHandler, "/ws/gallery").setAllowedOrigins("*");
        registry.addHandler(chatWsHandler, "/ws/chat").setAllowedOrigins("*");
    }
}
