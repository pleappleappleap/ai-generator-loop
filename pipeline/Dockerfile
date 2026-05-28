FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY build/lib/     lib/
COPY build/classes/ classes/
ENTRYPOINT ["java", "-cp", "classes:lib/*", "ai.image.pipeline.PipelineApplication"]
