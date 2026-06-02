package org.soxhlet.pipeline.config;

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

    @Bean("artifactScorerClient")
    public RestClient artifactScorerClient(
            @Value("${sidecars.artifact-scorer.url}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }

    @Bean("vlmScorerClient")
    public RestClient vlmScorerClient(
            @Value("${sidecars.vlm-scorer.url}") String url) {
        return RestClient.builder().baseUrl(url).build();
    }

    @Bean("tacticalLlmClient")
    public RestClient tacticalLlmClient(
            @Value("${tactical-llm.url}") String url) {
        return RestClient.builder()
                .baseUrl(url)
                .requestFactory(new HttpComponentsClientHttpRequestFactory())
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
}
