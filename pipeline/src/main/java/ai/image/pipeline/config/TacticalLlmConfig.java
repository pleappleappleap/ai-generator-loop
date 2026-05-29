package ai.image.pipeline.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

@Component
@ConfigurationProperties(prefix = "tactical-llm")
public class TacticalLlmConfig {

    private String url = "http://localhost:8080/v1";
    private String model = "default";
    private double temperature = 0.1;
    private int maxTokens = 512;
    private int maxTokensThinking = 4096;
    private Decisions decisions = new Decisions();

    public String getUrl() { return url; }
    public void setUrl(String url) { this.url = url; }
    public String getModel() { return model; }
    public void setModel(String model) { this.model = model; }
    public double getTemperature() { return temperature; }
    public void setTemperature(double temperature) { this.temperature = temperature; }
    public int getMaxTokens() { return maxTokens; }
    public void setMaxTokens(int maxTokens) { this.maxTokens = maxTokens; }
    public int getMaxTokensThinking() { return maxTokensThinking; }
    public void setMaxTokensThinking(int maxTokensThinking) { this.maxTokensThinking = maxTokensThinking; }
    public Decisions getDecisions() { return decisions; }
    public void setDecisions(Decisions decisions) { this.decisions = decisions; }

    public static class Decisions {
        private int maxRetries = 3;
        private int maxInpaints = 2;
        private double acceptVlmMeanMin = 7.0;
        private double acceptClipMin = 0.30;
        public int getMaxRetries() { return maxRetries; }
        public void setMaxRetries(int maxRetries) { this.maxRetries = maxRetries; }
        public int getMaxInpaints() { return maxInpaints; }
        public void setMaxInpaints(int maxInpaints) { this.maxInpaints = maxInpaints; }
        public double getAcceptVlmMeanMin() { return acceptVlmMeanMin; }
        public void setAcceptVlmMeanMin(double acceptVlmMeanMin) { this.acceptVlmMeanMin = acceptVlmMeanMin; }
        public double getAcceptClipMin() { return acceptClipMin; }
        public void setAcceptClipMin(double acceptClipMin) { this.acceptClipMin = acceptClipMin; }
    }
}
