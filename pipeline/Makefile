.PHONY: all resolve compile package test docker clean

JAVA   := java
JAVAC  := javac

IVY_JAR      := tools/ivy.jar
IVY_URL      := https://repo1.maven.org/maven2/org/apache/ivy/ivy/2.5.2/ivy-2.5.2.jar
JUNIT_JAR    := tools/junit-platform-console-standalone.jar
JUNIT_URL    := https://repo1.maven.org/maven2/org/junit/platform/junit-platform-console-standalone/1.12.2/junit-platform-console-standalone-1.12.2.jar

SRC      := src/main/java
RES      := src/main/resources
TEST_SRC := src/test/java
OUT      := build/classes
TEST_OUT := build/test-classes
LIB      := build/lib
TEST_LIB := build/test-lib

all: compile

# ── Bootstrap tools ────────────────────────────────────────────────────────────

$(IVY_JAR):
	mkdir -p tools
	curl -sL $(IVY_URL) -o $(IVY_JAR)

$(JUNIT_JAR):
	mkdir -p tools
	curl -sL $(JUNIT_URL) -o $(JUNIT_JAR)

# ── Dependency resolution ──────────────────────────────────────────────────────

$(LIB)/.resolved: ivy.xml ivysettings.xml $(IVY_JAR)
	mkdir -p $(LIB)
	$(JAVA) -jar $(IVY_JAR) -settings ivysettings.xml -ivy ivy.xml \
		-retrieve "$(LIB)/[artifact]-[revision].[ext]" -confs runtime
	touch $(LIB)/.resolved

$(TEST_LIB)/.resolved: ivy.xml ivysettings.xml $(IVY_JAR)
	mkdir -p $(TEST_LIB)
	$(JAVA) -jar $(IVY_JAR) -settings ivysettings.xml -ivy ivy.xml \
		-retrieve "$(TEST_LIB)/[artifact]-[revision].[ext]" -confs test
	touch $(TEST_LIB)/.resolved

resolve: $(LIB)/.resolved

# ── Compile main sources ───────────────────────────────────────────────────────

compile: $(LIB)/.resolved
	mkdir -p $(OUT) build
	find $(SRC) -name '*.java' > build/sources.txt
	$(JAVAC) --release 21 -cp "$(LIB)/*" -d $(OUT) @build/sources.txt
	cp -r $(RES)/. $(OUT)/

package: compile

# ── Test (compile + run) ───────────────────────────────────────────────────────

test: compile $(TEST_LIB)/.resolved $(JUNIT_JAR)
	mkdir -p $(TEST_OUT) build
	find $(TEST_SRC) -name '*.java' > build/test-sources.txt
	$(JAVAC) --release 21 -cp "$(LIB)/*:$(TEST_LIB)/*:$(OUT)" -d $(TEST_OUT) @build/test-sources.txt
	$(JAVA) -cp "$(OUT):$(TEST_OUT):$(LIB)/*:$(TEST_LIB)/*:$(JUNIT_JAR)" \
		org.junit.platform.console.ConsoleLauncher execute --scan-classpath=$(TEST_OUT)

# ── Docker image ───────────────────────────────────────────────────────────────

docker: compile
	docker build -t pipeline:latest .

# ── Clean ──────────────────────────────────────────────────────────────────────

clean:
	rm -rf build target
