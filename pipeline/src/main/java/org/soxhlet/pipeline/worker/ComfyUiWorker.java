package org.soxhlet.pipeline.worker;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.soxhlet.pipeline.config.ModelsConfig;
import org.soxhlet.pipeline.service.GalleryBroadcastService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.jms.annotation.JmsListener;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.client.RestClient;

import jakarta.annotation.PreDestroy;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.logging.Logger;
import java.util.stream.StreamSupport;

@Component
public class ComfyUiWorker {

    private static final Logger log = Logger.getLogger(ComfyUiWorker.class.getName());

    @Value("${comfyui.url:http://localhost:8188}")
    private String comfyuiUrl;

    @Value("${comfyui.ws:ws://localhost:8188/ws}")
    private String comfyuiWs;

    private final TransactionTemplate requiresNew;
    private final NamedParameterJdbcTemplate jdbc;
    private final JmsTemplate jmsTemplate;
    private final ObjectMapper mapper;
    private final ModelsConfig modelsConfig;
    private final RestClient comfyClient;
    private final GalleryBroadcastService galleryBroadcast;
    private final Path workflowsDir;
    private final HttpClient httpClient;

    public ComfyUiWorker(
            PlatformTransactionManager txManager,
            NamedParameterJdbcTemplate jdbc,
            JmsTemplate jmsTemplate,
            ObjectMapper mapper,
            ModelsConfig modelsConfig,
            GalleryBroadcastService galleryBroadcast,
            @Value("${comfyui.url:http://localhost:8188}") String comfyuiUrlParam,
            @Value("${ui.workflows-dir:loop/workflows}") String workflowsDirParam) {
        this.requiresNew = new TransactionTemplate(txManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.jdbc = jdbc;
        this.jmsTemplate = jmsTemplate;
        this.mapper = mapper;
        this.modelsConfig = modelsConfig;
        this.galleryBroadcast = galleryBroadcast;
        this.comfyClient = RestClient.builder().baseUrl(comfyuiUrlParam).build();
        this.workflowsDir = Path.of(workflowsDirParam).toAbsolutePath().normalize();
        this.httpClient = HttpClient.newHttpClient();
    }

    @PreDestroy
    public void shutdown() {
        httpClient.close();
    }

    // ── JMS listeners — stage only, return immediately ─────────────────────────
    // Tx1: JMS ACK + staging INSERT commit together when the @Transactional method
    // returns. The poller below picks up the row and does the long ComfyUI work.

    @Transactional
    @JmsListener(destination = "loop.generate")
    public void onGenerate(String body) throws Exception { stageGeneration(body, "generate"); }

    @Transactional
    @JmsListener(destination = "loop.retry")
    public void onRetry(String body) throws Exception { stageGeneration(body, "retry"); }

    @Transactional
    @JmsListener(destination = "loop.inpaint")
    public void onInpaint(String body) throws Exception { stageGeneration(body, "inpaint"); }

    private void stageGeneration(String body, String type) throws Exception {
        JsonNode msg = mapper.readTree(body);
        jdbc.update(
                "INSERT INTO pending_generations (image_uuid, session_uuid, type, payload) " +
                "VALUES (:id::uuid, :sess::uuid, :type, :payload::jsonb) ON CONFLICT DO NOTHING",
                Map.of(
                        "id", msg.get("image_uuid").asText(),
                        "sess", msg.get("session_uuid").asText(),
                        "type", type,
                        "payload", body));
    }

    // ── Polling loop — processes staged work (normal path + crash recovery) ────
    // fixedDelay ensures only one execution is in flight at a time; the long
    // ComfyUI call naturally gates the next poll.

    @Scheduled(fixedDelay = 2000)
    public void pollAndProcess() {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT image_uuid::text, prompt_id, payload::text " +
                "FROM pending_generations LIMIT 1",
                Map.of());
        if (rows.isEmpty()) return;
        Map<String, Object> row = rows.get(0);
        String imageUuid = (String) row.get("image_uuid");
        String promptId  = (String) row.get("prompt_id");
        String payloadJson = (String) row.get("payload");
        try {
            JsonNode payload = mapper.readTree(payloadJson);
            resumeGeneration(imageUuid, promptId, payload);
        } catch (Exception e) {
            log.warning("Generation processing failed for " + imageUuid + ": " + e.getMessage());
        }
    }

    private void resumeGeneration(String imageUuid, String existingPromptId, JsonNode msg) throws Exception {
        String prompt = msg.path("prompt").asText(null);
        String workflowPath = msg.path("workflow_path").asText("");
        JsonNode workflowParams = msg.has("workflow_params") ? msg.get("workflow_params") : mapper.createObjectNode();
        JsonNode graphOps = msg.has("graph_ops") ? msg.get("graph_ops") : mapper.createArrayNode();
        JsonNode lorasNode = msg.has("loras") ? msg.get("loras") : mapper.createArrayNode();
        String modelType = msg.path("model_type").asText(null);
        String sessionUuid = msg.path("session_uuid").asText("");
        int sequenceNumber = msg.path("sequence_number").asInt(0);
        String workflowId = nullIfBlank(msg.path("workflow_id").asText(""));
        String conversationId = nullIfBlank(msg.path("conversation_id").asText(""));
        String workflowParamsJson = mapper.writeValueAsString(workflowParams);

        // Load and patch workflow
        ObjectNode workflow = loadWorkflow(workflowPath);
        String effectiveModelType = modelType != null ? modelType : detectModelType(workflow);
        workflow = injectLoras(workflow, lorasNode, effectiveModelType);
        workflow = applyWorkflowParams(workflow, (ObjectNode) workflowParams);
        workflow = randomizeSeed(workflow);
        if (graphOps.isArray() && graphOps.size() > 0) {
            workflow = applyGraphOps(workflow, (ArrayNode) graphOps);
        }
        // patchCheckpoint runs last so it can validate/override any checkpoint set by graph ops
        workflow = patchCheckpoint(workflow, effectiveModelType);

        String promptId = existingPromptId;
        String clientId = null;

        if (promptId == null) {
            // Submit to ComfyUI
            String[] result = submitWorkflow(workflow, prompt);
            promptId = result[0];
            clientId = result[1];
            final String finalPromptId = promptId;
            requiresNew.execute(status -> {
                jdbc.update(
                        "UPDATE pending_generations SET prompt_id = :pid WHERE image_uuid = :id::uuid",
                        Map.of("pid", finalPromptId, "id", imageUuid));
                return null;
            });
            log.info("Submitted ComfyUI prompt_id=" + promptId + " for " + imageUuid);
        } else {
            log.info("Resuming existing ComfyUI prompt_id=" + promptId + " for " + imageUuid);
        }

        // Wait for ComfyUI completion (WebSocket + history fallback)
        waitForCompletion(promptId, clientId);

        // Get output path
        String outputPath = getOutputPath(promptId);
        log.info("ComfyUI output: " + outputPath + " for " + imageUuid);

        // Tx2 (REQUIRES_NEW): delete staging, insert initial images row, publish loop.generated
        final String finalPromptId2 = promptId;
        final String finalOutput = outputPath;
        final String finalWorkflowId = workflowId;
        final String finalConvId = conversationId;
        requiresNew.execute(status -> {
            try {
                jdbc.update("DELETE FROM pending_generations WHERE image_uuid = :id::uuid",
                        Map.of("id", imageUuid));

                jdbc.update(
                        """
                        INSERT INTO images (
                            image_uuid, session_uuid, sequence_number, prompt,
                            workflow_path, workflow_params, workflow_id, conversation_id, image_path
                        ) VALUES (
                            :imageUuid::uuid, :sessionUuid::uuid, :seqNum, :prompt,
                            :workflowPath, :workflowParams::jsonb, :workflowId::uuid, :conversationId::uuid,
                            :imagePath
                        ) ON CONFLICT DO NOTHING
                        """,
                        new MapSqlParameterSource()
                                .addValue("imageUuid", imageUuid)
                                .addValue("sessionUuid", sessionUuid)
                                .addValue("seqNum", sequenceNumber)
                                .addValue("prompt", prompt)
                                .addValue("workflowPath", workflowPath)
                                .addValue("workflowParams", mapper.writeValueAsString(workflowParams))
                                .addValue("workflowId", finalWorkflowId)
                                .addValue("conversationId", finalConvId)
                                .addValue("imagePath", finalOutput));

                // Ensure budget row exists for this session
                ensureBudget(sessionUuid, finalWorkflowId, finalConvId);

                // Publish to loop.generated for the Scorer
                ObjectNode gen = mapper.createObjectNode();
                gen.put("image_uuid", imageUuid);
                gen.put("session_uuid", sessionUuid);
                gen.put("sequence_number", sequenceNumber);
                gen.put("prompt", prompt == null ? "" : prompt);
                gen.put("prompt_id", finalPromptId2);
                gen.put("image_path", finalOutput);
                gen.put("workflow_path", workflowPath);
                gen.put("workflow_id", workflowId != null ? workflowId : "");
                gen.put("conversation_id", conversationId != null ? conversationId : "");
                gen.set("workflow_params", workflowParams);
                jmsTemplate.convertAndSend("loop.generated", mapper.writeValueAsString(gen));
                log.info("Published loop.generated for " + imageUuid);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
            return null;
        });

        // Broadcast image_ready to gallery WebSocket clients (after Tx2 commits)
        galleryBroadcast.imageReady(imageUuid, workflowId, conversationId, sequenceNumber);
    }

    private void ensureBudget(String sessionUuid, String workflowId, String conversationId) {
        int existing = jdbc.queryForObject(
                "SELECT COUNT(*) FROM budget WHERE session_uuid = :id::uuid",
                Map.of("id", sessionUuid), Integer.class);
        if (existing > 0) return;

        int maxRetries = 3;
        int maxInpaints = 2;
        if (workflowId != null) {
            List<Map<String, Object>> wf = jdbc.queryForList(
                    "SELECT max_retries, max_inpaints FROM workflows WHERE workflow_id = :id::uuid",
                    Map.of("id", workflowId));
            if (!wf.isEmpty()) {
                maxRetries = ((Number) wf.get(0).get("max_retries")).intValue();
                maxInpaints = ((Number) wf.get(0).get("max_inpaints")).intValue();
            }
        }
        jdbc.update(
                """
                INSERT INTO budget (session_uuid, workflow_id, conversation_id,
                    max_retries, max_inpaints, expires_at)
                VALUES (:sess::uuid, :wfId::uuid, :convId::uuid,
                    :maxRetries, :maxInpaints, now() + interval '24 hours')
                ON CONFLICT DO NOTHING
                """,
                new MapSqlParameterSource()
                        .addValue("sess", sessionUuid)
                        .addValue("wfId", workflowId)
                        .addValue("convId", conversationId)
                        .addValue("maxRetries", maxRetries)
                        .addValue("maxInpaints", maxInpaints));
    }

    // ── Workflow patching ──────────────────────────────────────────────────────

    private ObjectNode loadWorkflow(String workflowPath) throws Exception {
        Path resolved = Path.of(workflowPath).toAbsolutePath().normalize();
        if (!resolved.startsWith(workflowsDir)) {
            throw new SecurityException("Workflow path outside allowed directory: " + workflowPath);
        }
        String json = Files.readString(resolved);
        return (ObjectNode) mapper.readTree(json);
    }

    private String detectModelType(ObjectNode workflow) {
        for (JsonNode node : workflow) {
            String ct = node.path("class_type").asText("");
            if ("UNETLoader".equals(ct) || "FluxGuidance".equals(ct)) return "flux";
        }
        for (JsonNode node : workflow) {
            if ("CheckpointLoaderSimple".equals(node.path("class_type").asText(""))) return "sdxl";
        }
        return "unknown";
    }

    private String resolveCheckpoint(String configured) {
        if (configured != null && !configured.isBlank()) return configured;
        try {
            JsonNode info = comfyClient.get().uri("/object_info/CheckpointLoaderSimple")
                    .retrieve().body(JsonNode.class);
            JsonNode list = info.path("CheckpointLoaderSimple").path("input")
                    .path("required").path("ckpt_name").path(0);
            if (list.isArray() && list.size() > 0) return list.get(0).asText();
        } catch (Exception e) {
            log.warning("Could not query ComfyUI checkpoints: " + e.getMessage());
        }
        return null;
    }

    private Set<String> fetchInstalledCheckpoints() {
        try {
            JsonNode info = comfyClient.get().uri("/object_info/CheckpointLoaderSimple")
                    .retrieve().body(JsonNode.class);
            JsonNode list = info.path("CheckpointLoaderSimple").path("input")
                    .path("required").path("ckpt_name").path(0);
            Set<String> names = new HashSet<>();
            if (list.isArray()) list.forEach(n -> names.add(n.asText()));
            return names;
        } catch (Exception e) {
            log.warning("Could not fetch installed checkpoints: " + e.getMessage());
            return Set.of();
        }
    }

    private ObjectNode patchCheckpoint(ObjectNode workflow, String modelType) {
        ObjectNode w = workflow.deepCopy();
        if ("sd15".equals(modelType) || "sdxl".equals(modelType)) {
            Set<String> installed = fetchInstalledCheckpoints();
            // If the workflow already has a valid installed checkpoint (e.g. set via graph op), keep it
            boolean alreadyValid = false;
            for (JsonNode node : w) {
                if ("CheckpointLoaderSimple".equals(node.path("class_type").asText(""))) {
                    String current = node.path("inputs").path("ckpt_name").asText("");
                    if (!current.isBlank() && installed.contains(current)) {
                        alreadyValid = true;
                        break;
                    }
                }
            }
            if (!alreadyValid) {
                String configured = "sd15".equals(modelType)
                        ? modelsConfig.getSd15().getCheckpoint()
                        : modelsConfig.getSdxl().getCheckpoint();
                // Fall back to sd15 checkpoint when sdxl is unconfigured
                if ((configured == null || configured.isBlank()) && !modelsConfig.getSd15().getCheckpoint().isBlank()) {
                    configured = modelsConfig.getSd15().getCheckpoint();
                }
                String ckpt = resolveCheckpoint(configured);
                if (ckpt != null) {
                    final String finalCkpt = ckpt;
                    w.fields().forEachRemaining(e -> {
                        if ("CheckpointLoaderSimple".equals(e.getValue().path("class_type").asText(""))) {
                            ((ObjectNode) e.getValue().path("inputs")).put("ckpt_name", finalCkpt);
                        }
                    });
                    log.info("patchCheckpoint: overrode invalid checkpoint with '" + ckpt + "'");
                }
            }
        } else if ("flux".equals(modelType)) {
            ModelsConfig.Flux flux = modelsConfig.getFlux();
            w.fields().forEachRemaining(e -> {
                JsonNode node = e.getValue();
                String ct = node.path("class_type").asText("");
                ObjectNode inputs = (ObjectNode) node.path("inputs");
                if ("UNETLoader".equals(ct) && !flux.getUnet().isBlank()) inputs.put("unet_name", flux.getUnet());
                else if ("VAELoader".equals(ct) && !flux.getVae().isBlank()) inputs.put("vae_name", flux.getVae());
                else if ("DualCLIPLoader".equals(ct)) {
                    if (!flux.getClipL().isBlank()) inputs.put("clip_name1", flux.getClipL());
                    if (!flux.getT5xxl().isBlank()) inputs.put("clip_name2", flux.getT5xxl());
                }
            });
        }
        return w;
    }

    private ObjectNode injectLoras(ObjectNode workflow, JsonNode lorasNode, String modelType) {
        if (!lorasNode.isArray() || lorasNode.size() == 0) return workflow;

        // Build registry: name → config entry
        Map<String, ModelsConfig.LoraEntry> registry = new HashMap<>();
        for (ModelsConfig.LoraEntry e : modelsConfig.lorasFor(modelType)) {
            registry.put(e.getName(), e);
        }

        List<JsonNode> resolvedLoras = new ArrayList<>();
        for (JsonNode req : lorasNode) {
            String name = req.path("name").asText("");
            ModelsConfig.LoraEntry entry = registry.get(name);
            ObjectNode resolved = mapper.createObjectNode();
            if (entry != null) {
                resolved.put("filename", entry.getFilename());
                resolved.put("strength_model", req.has("strength_model") ? req.get("strength_model").asDouble() : entry.getDefaultStrength());
                resolved.put("strength_clip",  req.has("strength_clip")  ? req.get("strength_clip").asDouble()  : entry.getDefaultStrength());
                resolved.put("name", entry.getName());
            } else {
                // Treat name as filename directly (for manually-installed LoRAs not in config)
                log.info("LoRA '" + name + "' not in config registry — using as filename directly");
                resolved.put("filename", name);
                resolved.put("strength_model", req.has("strength_model") ? req.get("strength_model").asDouble() : 1.0);
                resolved.put("strength_clip",  req.has("strength_clip")  ? req.get("strength_clip").asDouble()  : 1.0);
                resolved.put("name", name);
            }
            resolvedLoras.add(resolved);
        }
        if (resolvedLoras.isEmpty()) return workflow;

        ObjectNode w = workflow.deepCopy();

        // Find source nodes
        String modelNodeId = null, clipNodeId = null;
        int modelOutputIdx = 0, clipOutputIdx = 0;
        if ("sdxl".equals(modelType)) {
            modelNodeId = findNodeByClass(w, "CheckpointLoaderSimple");
            clipNodeId = modelNodeId;
            modelOutputIdx = 0; clipOutputIdx = 1;
        } else if ("flux".equals(modelType)) {
            modelNodeId = findNodeByClass(w, "UNETLoader");
            clipNodeId = findNodeByClass(w, "DualCLIPLoader");
        }
        if (modelNodeId == null || clipNodeId == null) return w;

        int maxId = StreamSupport.stream(
                ((Iterable<String>) w::fieldNames).spliterator(), false)
                .filter(k -> k.chars().allMatch(Character::isDigit))
                .mapToInt(Integer::parseInt).max().orElse(100);

        List<String> newLoraIds = new ArrayList<>();
        String[] currentModel = {modelNodeId, String.valueOf(modelOutputIdx)};
        String[] currentClip  = {clipNodeId,  String.valueOf(clipOutputIdx)};

        for (JsonNode lora : resolvedLoras) {
            maxId++;
            String loraId = String.valueOf(maxId);
            newLoraIds.add(loraId);
            ObjectNode loraNode = mapper.createObjectNode();
            loraNode.put("class_type", "LoraLoader");
            ObjectNode inputs = mapper.createObjectNode();
            inputs.set("model", mapper.createArrayNode().add(currentModel[0]).add(Integer.parseInt(currentModel[1])));
            inputs.set("clip",  mapper.createArrayNode().add(currentClip[0]).add(Integer.parseInt(currentClip[1])));
            inputs.put("lora_name",      lora.get("filename").asText());
            inputs.put("strength_model", lora.get("strength_model").asDouble());
            inputs.put("strength_clip",  lora.get("strength_clip").asDouble());
            loraNode.set("inputs", inputs);
            ObjectNode meta = mapper.createObjectNode();
            meta.put("title", "LoRA: " + lora.get("name").asText());
            loraNode.set("_meta", meta);
            w.set(loraId, loraNode);
            currentModel[0] = loraId; currentModel[1] = "0";
            currentClip[0]  = loraId; currentClip[1]  = "1";
        }

        // Redirect pre-existing nodes that consumed original source outputs
        final String origModelNodeId = modelNodeId;
        final String origClipNodeId  = clipNodeId;
        final int origModelIdx = modelOutputIdx;
        final int origClipIdx  = clipOutputIdx;
        for (Map.Entry<String, JsonNode> e : iterable(w.fields())) {
            if (newLoraIds.contains(e.getKey())) continue;
            ObjectNode inputs = (ObjectNode) e.getValue().path("inputs");
            if (inputs.isMissingNode()) continue;
            for (Map.Entry<String, JsonNode> inp : iterable(inputs.fields())) {
                JsonNode val = inp.getValue();
                if (!val.isArray() || val.size() != 2) continue;
                String refId = val.get(0).asText();
                int refPort  = val.get(1).asInt();
                if (refId.equals(origModelNodeId) && refPort == origModelIdx) {
                    inputs.set(inp.getKey(), mapper.createArrayNode().add(currentModel[0]).add(Integer.parseInt(currentModel[1])));
                } else if (refId.equals(origClipNodeId) && refPort == origClipIdx) {
                    inputs.set(inp.getKey(), mapper.createArrayNode().add(currentClip[0]).add(Integer.parseInt(currentClip[1])));
                }
            }
        }
        return w;
    }

    private ObjectNode randomizeSeed(ObjectNode workflow) {
        ObjectNode w = workflow.deepCopy();
        for (JsonNode node : w) {
            JsonNode inputs = node.path("inputs");
            if (inputs.has("seed") && inputs.get("seed").asLong(-1) < 0) {
                ((ObjectNode) inputs).put("seed", (long) (Math.random() * Long.MAX_VALUE));
            }
        }
        return w;
    }

    private ObjectNode applyWorkflowParams(ObjectNode workflow, ObjectNode params) {
        if (params == null || params.size() == 0) return workflow;
        ObjectNode w = workflow.deepCopy();
        for (JsonNode node : w) {
            String ct = node.path("class_type").asText("");
            if (params.has(ct)) {
                ObjectNode inputs = (ObjectNode) node.path("inputs");
                params.get(ct).fields().forEachRemaining(e -> inputs.set(e.getKey(), e.getValue()));
            }
        }
        return w;
    }

    private ObjectNode applyGraphOps(ObjectNode workflow, ArrayNode ops) {
        ObjectNode w = workflow.deepCopy();
        int maxId = StreamSupport.stream(
                ((Iterable<String>) w::fieldNames).spliterator(), false)
                .filter(k -> k.chars().allMatch(Character::isDigit))
                .mapToInt(Integer::parseInt).max().orElse(100);
        Map<String, String> idMap = new HashMap<>();

        for (JsonNode op : ops) {
            String opType = op.path("op").asText("");
            switch (opType) {
                case "add_node" -> {
                    String placeholder = op.path("id").asText("");
                    maxId++;
                    String realId = String.valueOf(maxId);
                    if (!placeholder.isBlank()) idMap.put(placeholder, realId);
                    ObjectNode node = mapper.createObjectNode();
                    node.put("class_type", op.get("class_type").asText());
                    ObjectNode inputs = mapper.createObjectNode();
                    if (op.has("inputs")) {
                        op.get("inputs").fields().forEachRemaining(e ->
                            inputs.set(e.getKey(), resolveRef(e.getValue(), idMap)));
                    }
                    node.set("inputs", inputs);
                    if (op.has("_meta")) node.set("_meta", op.get("_meta"));
                    w.set(realId, node);
                }
                case "remove_node" -> {
                    String nodeId = op.path("id").asText();
                    Map<String, JsonNode> rewire = new HashMap<>();
                    if (op.has("rewire")) op.get("rewire").fields().forEachRemaining(e -> rewire.put(e.getKey(), e.getValue()));
                    for (JsonNode node : w) {
                        if (node.has("inputs")) {
                            ObjectNode inputs = (ObjectNode) node.get("inputs");
                            for (Map.Entry<String, JsonNode> inp : iterable(inputs.fields())) {
                                JsonNode val = inp.getValue();
                                if (val.isArray() && val.size() == 2 && nodeId.equals(val.get(0).asText())) {
                                    String port = val.get(1).asText();
                                    if (rewire.containsKey(port)) inputs.set(inp.getKey(), rewire.get(port));
                                }
                            }
                        }
                    }
                    w.remove(nodeId);
                }
                case "rewire" -> {
                    String nodeId = String.valueOf(op.path("id").asText());
                    if (w.has(nodeId)) {
                        ObjectNode inputs = (ObjectNode) w.get(nodeId).path("inputs");
                        inputs.set(op.get("input").asText(), resolveRef(op.get("to"), idMap));
                    }
                }
                case "set_input" -> {
                    String inputKey = op.path("input").asText("");
                    JsonNode value  = op.path("value");
                    // Target by numeric id or by class_type
                    String nodeId   = op.path("id").asText("");
                    String nodeClass = op.path("class_type").asText("");
                    w.fields().forEachRemaining(e -> {
                        JsonNode node = e.getValue();
                        boolean match = (!nodeId.isBlank() && nodeId.equals(e.getKey()))
                                || (!nodeClass.isBlank() && nodeClass.equals(node.path("class_type").asText("")));
                        if (match && node.has("inputs")) {
                            ((ObjectNode) node.path("inputs")).set(inputKey, value);
                        }
                    });
                }
            }
        }
        return w;
    }

    private JsonNode resolveRef(JsonNode val, Map<String, String> idMap) {
        if (val.isArray() && val.size() == 2) {
            String ref = val.get(0).asText();
            return mapper.createArrayNode().add(idMap.getOrDefault(ref, ref)).add(val.get(1).asInt());
        }
        return val;
    }

    private String findNodeByClass(ObjectNode workflow, String classType) {
        for (Map.Entry<String, JsonNode> e : iterable(workflow.fields())) {
            if (classType.equals(e.getValue().path("class_type").asText(""))) return e.getKey();
        }
        return null;
    }

    // ── ComfyUI HTTP + WebSocket ───────────────────────────────────────────────

    private String[] submitWorkflow(ObjectNode workflow, String promptOverride) throws Exception {
        if (promptOverride != null && !promptOverride.isBlank()) {
            for (JsonNode node : workflow) {
                if ("CLIPTextEncode".equals(node.path("class_type").asText(""))) {
                    String title = node.path("_meta").path("title").asText("").toLowerCase();
                    if (title.contains("positive")) {
                        ((ObjectNode) node.path("inputs")).put("text", promptOverride);
                    }
                }
            }
        }
        String clientId = UUID.randomUUID().toString();
        ObjectNode payload = mapper.createObjectNode();
        payload.set("prompt", workflow);
        payload.put("client_id", clientId);

        JsonNode response = comfyClient.post().uri("/prompt")
                .contentType(MediaType.APPLICATION_JSON)
                .body(payload)
                .retrieve()
                .body(JsonNode.class);
        return new String[]{response.get("prompt_id").asText(), clientId};
    }

    private void waitForCompletion(String promptId, String clientId) throws Exception {
        // Fast path: already done (cache hit or previously submitted)
        if (historyComplete(promptId)) return;

        if (clientId == null) {
            // Recovery path: poll history only
            for (int i = 0; i < 360; i++) {
                Thread.sleep(2000);
                if (historyComplete(promptId)) return;
            }
            throw new RuntimeException("Timed out waiting for ComfyUI prompt " + promptId);
        }

        // WebSocket path with history fallback
        CompletableFuture<Void> done = new CompletableFuture<>();
        URI wsUri = URI.create(comfyuiWs + "?clientId=" + clientId);
        httpClient.newWebSocketBuilder().buildAsync(wsUri, new WebSocket.Listener() {
            @Override
            public java.util.concurrent.CompletionStage<?> onText(WebSocket ws, CharSequence data, boolean last) {
                try {
                    JsonNode msg = mapper.readTree(data.toString());
                    if ("executed".equals(msg.path("type").asText())
                            && promptId.equals(msg.path("data").path("prompt_id").asText())) {
                        done.complete(null);
                        ws.sendClose(WebSocket.NORMAL_CLOSURE, "done");
                    }
                } catch (Exception ignored) {}
                ws.request(1);
                return null;
            }
            @Override
            public void onError(WebSocket ws, Throwable error) { done.completeExceptionally(error); }
        }).thenAccept(ws -> ws.request(1));

        // Poll history every 10 s as fallback (WS event may be missed on connection race)
        for (int tick = 0; tick < 120 && !done.isDone(); tick++) {
            try {
                done.get(10, TimeUnit.SECONDS);
            } catch (TimeoutException ignored) {}
            if (!done.isDone() && historyComplete(promptId)) done.complete(null);
        }
        if (!done.isDone()) throw new RuntimeException("Timed out waiting for ComfyUI prompt " + promptId);
    }

    private boolean historyComplete(String promptId) {
        try {
            JsonNode history = comfyClient.get().uri("/history/" + promptId).retrieve().body(JsonNode.class);
            return history != null && history.has(promptId) && history.get(promptId).has("outputs");
        } catch (Exception e) {
            return false;
        }
    }

    private String getOutputPath(String promptId) throws Exception {
        JsonNode history = comfyClient.get().uri("/history/" + promptId).retrieve().body(JsonNode.class);
        JsonNode outputs = history.path(promptId).path("outputs");
        for (JsonNode nodeOutput : outputs) {
            if (nodeOutput.has("images")) {
                JsonNode img = nodeOutput.get("images").get(0);
                return comfyuiUrl + "/view"
                        + "?filename=" + URLEncoder.encode(img.get("filename").asText(), StandardCharsets.UTF_8)
                        + "&subfolder=" + URLEncoder.encode(img.get("subfolder").asText(), StandardCharsets.UTF_8)
                        + "&type=" + URLEncoder.encode(img.get("type").asText(), StandardCharsets.UTF_8);
            }
        }
        throw new RuntimeException("No image output found for prompt " + promptId);
    }

    // ── Utilities ──────────────────────────────────────────────────────────────

    private static String nullIfBlank(String s) {
        return (s == null || s.isBlank()) ? null : s;
    }

    private static <T> Iterable<T> iterable(Iterator<T> it) {
        return () -> it;
    }
}
