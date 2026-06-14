package org.soxhlet.pipeline.worker;

public sealed interface AgentEvent permits AgentEvent.UserMessage, AgentEvent.Verdict {
    record UserMessage(String text) implements AgentEvent {}
    record Verdict(String imageUuid, String payloadJson) implements AgentEvent {}
}
