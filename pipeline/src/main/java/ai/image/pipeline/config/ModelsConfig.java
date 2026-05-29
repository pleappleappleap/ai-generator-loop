package ai.image.pipeline.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Component
@ConfigurationProperties(prefix = "models")
public class ModelsConfig {

    private Sdxl sdxl = new Sdxl();
    private Flux flux = new Flux();
    private Map<String, List<LoraEntry>> loras = new HashMap<>();

    public Sdxl getSdxl() { return sdxl; }
    public void setSdxl(Sdxl sdxl) { this.sdxl = sdxl; }
    public Flux getFlux() { return flux; }
    public void setFlux(Flux flux) { this.flux = flux; }
    public Map<String, List<LoraEntry>> getLoras() { return loras; }
    public void setLoras(Map<String, List<LoraEntry>> loras) { this.loras = loras; }

    public List<LoraEntry> lorasFor(String modelType) {
        return loras.getOrDefault(modelType, new ArrayList<>());
    }

    public static class Sdxl {
        private String checkpoint = "";
        public String getCheckpoint() { return checkpoint; }
        public void setCheckpoint(String checkpoint) { this.checkpoint = checkpoint; }
    }

    public static class Flux {
        private String unet = "";
        private String vae = "";
        private String clipL = "";
        private String t5xxl = "";
        public String getUnet() { return unet; }
        public void setUnet(String unet) { this.unet = unet; }
        public String getVae() { return vae; }
        public void setVae(String vae) { this.vae = vae; }
        public String getClipL() { return clipL; }
        public void setClipL(String clipL) { this.clipL = clipL; }
        public String getT5xxl() { return t5xxl; }
        public void setT5xxl(String t5xxl) { this.t5xxl = t5xxl; }
    }

    public static class LoraEntry {
        private String name = "";
        private String filename = "";
        private String description = "";
        private double defaultStrength = 1.0;
        private String triggerWord = "";
        public String getName() { return name; }
        public void setName(String name) { this.name = name; }
        public String getFilename() { return filename; }
        public void setFilename(String filename) { this.filename = filename; }
        public String getDescription() { return description; }
        public void setDescription(String description) { this.description = description; }
        public double getDefaultStrength() { return defaultStrength; }
        public void setDefaultStrength(double defaultStrength) { this.defaultStrength = defaultStrength; }
        public String getTriggerWord() { return triggerWord; }
        public void setTriggerWord(String triggerWord) { this.triggerWord = triggerWord; }
    }
}
