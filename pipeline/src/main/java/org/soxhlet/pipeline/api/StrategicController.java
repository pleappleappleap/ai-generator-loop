package org.soxhlet.pipeline.api;

import org.soxhlet.pipeline.worker.StrategicLlmCaller;
import org.soxhlet.pipeline.worker.StrategicLlmCaller.SessionResult;
import org.soxhlet.pipeline.worker.StrategicLlmCaller.State;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.util.LinkedHashMap;
import java.util.Map;

@RestController
@RequestMapping("/strategic")
public class StrategicController {

    private final StrategicLlmCaller caller;

    public StrategicController(StrategicLlmCaller caller) {
        this.caller = caller;
    }

    @GetMapping({"", "/"})
    public ResponseEntity<Void> ui() {
        return ResponseEntity.status(HttpStatus.FOUND)
                .location(URI.create("/strategic/index.html"))
                .build();
    }

    @PostMapping("/run")
    public ResponseEntity<Map<String, Object>> run() {
        if (caller.getState() == State.RUNNING) {
            return ResponseEntity.ok(Map.of("ok", false, "error", "session already running"));
        }
        SessionResult result = caller.runManualSession();
        return ResponseEntity.ok(resultToMap(result));
    }

    @GetMapping("/status")
    public Map<String, Object> status() {
        State state = caller.getState();
        SessionResult last = caller.getLastResult();
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("state", state.name().toLowerCase());
        if (last != null) {
            resp.put("north_star",        last.northStar());
            resp.put("session_direction", last.sessionDirection());
            resp.put("analysis",          last.analysis());
            resp.put("reasoning",         last.reasoning());
            resp.put("error",             last.error());
        }
        return resp;
    }

    private Map<String, Object> resultToMap(SessionResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok",               r.state() == State.COMPLETE);
        m.put("state",            r.state().name().toLowerCase());
        m.put("north_star",       r.northStar());
        m.put("session_direction",r.sessionDirection());
        m.put("analysis",         r.analysis());
        m.put("reasoning",        r.reasoning());
        m.put("error",            r.error());
        return m;
    }
}
