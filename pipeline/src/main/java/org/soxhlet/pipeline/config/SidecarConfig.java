package org.soxhlet.pipeline.config;

import org.apache.hc.client5.http.config.RequestConfig;
import org.apache.hc.client5.http.impl.classic.HttpClients;
import org.apache.hc.core5.util.Timeout;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.HttpComponentsClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

@Configuration
public class SidecarConfig {

    @Bean("clipScorerClient")
    public RestClient clipScorerClient(
            @Value("${sidecars.clip-scorer.url}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }

    @Bean("vlmScorerClient")
    public RestClient vlmScorerClient(
            @Value("${sidecars.vlm-scorer.url}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }

    @Bean("tacticalLlmClient")
    public RestClient tacticalLlmClient(
            @Value("${tactical-llm.url}") String url,
            @Value("${tactical-llm.response-timeout-seconds:120}") int timeoutSeconds) {
        var factory = new HttpComponentsClientHttpRequestFactory(
                HttpClients.custom()
                        .setDefaultRequestConfig(RequestConfig.custom()
                                .setResponseTimeout(Timeout.ofSeconds(timeoutSeconds))
                                .build())
                        .build());
        return RestClient.builder()
                .baseUrl(url)
                .requestFactory(factory)
                .build();
    }

    @Bean("strategicLlmClient")
    public RestClient strategicLlmClient(
            @Value("${strategic-llm.url}") String url) {
        return RestClient.builder()
                .baseUrl(url)
                .requestFactory(new HttpComponentsClientHttpRequestFactory())
                .build();
    }

    @Bean("comfyuiClient")
    public RestClient comfyuiClient(
            @Value("${comfyui.url:http://localhost:12006}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }

    @Bean("searxngClient")
    public RestClient searxngClient(
            @Value("${searxng.url:http://localhost:12010}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }
}
