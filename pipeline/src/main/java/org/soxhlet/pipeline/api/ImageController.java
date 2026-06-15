package org.soxhlet.pipeline.api;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

@RestController
public class ImageController {

    private static final Logger log = Logger.getLogger(ImageController.class.getName());

    private final NamedParameterJdbcTemplate jdbc;
    private final RestClient httpClient;

    public ImageController(
            NamedParameterJdbcTemplate jdbc,
            @Value("${comfyui.url:http://localhost:8188}") String comfyuiUrl) {
        this.jdbc = jdbc;
        this.httpClient = RestClient.builder().build();
    }

    @GetMapping("/image/{imageUuid}")
    public ResponseEntity<byte[]> getImage(@PathVariable String imageUuid) {
        List<String> paths = jdbc.queryForList(
                "SELECT image_path FROM images WHERE image_uuid = :id AND image_path IS NOT NULL",
                Map.of("id", java.util.UUID.fromString(imageUuid)), String.class);

        if (paths.isEmpty()) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND)
                    .body("image not found or not yet generated".getBytes());
        }

        String imagePath = paths.get(0);

        try {
            if (imagePath.startsWith("http")) {
                byte[] data = httpClient.get()
                        .uri(imagePath)
                        .retrieve()
                        .body(byte[].class);
                return ResponseEntity.ok()
                        .contentType(MediaType.IMAGE_PNG)
                        .body(data);
            }

            Path file = Path.of(imagePath);
            if (!Files.exists(file)) {
                return ResponseEntity.status(HttpStatus.NOT_FOUND)
                        .body("image file not found on disk".getBytes());
            }
            byte[] data = Files.readAllBytes(file);
            String contentType = Files.probeContentType(file);
            MediaType mediaType = contentType != null
                    ? MediaType.parseMediaType(contentType) : MediaType.IMAGE_PNG;
            return ResponseEntity.ok().contentType(mediaType).body(data);

        } catch (Exception e) {
            log.warning("Failed to serve image " + imageUuid + ": " + e.getMessage());
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                    .body(("error: " + e.getMessage()).getBytes());
        }
    }
}
