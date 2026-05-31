package org.soxhlet.pipeline.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

@Component
@ConfigurationProperties(prefix = "strategic-llm")
public class StrategicLlmConfig {

    private String url = "http://localhost:8081/v1";
    private String model = "default";
    private String systemPromptFile = "strategic-llm/system_prompt.md";

    public String getUrl() { return url; }
    public void setUrl(String url) { this.url = url; }
    public String getModel() { return model; }
    public void setModel(String model) { this.model = model; }
    public String getSystemPromptFile() { return systemPromptFile; }
    public void setSystemPromptFile(String f) { this.systemPromptFile = f; }
}
