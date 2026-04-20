# AI Image Generation Pipeline

An autonomous image generation pipeline using Stable Diffusion XL,
multi-dimensional scoring, and a tactical LLM feedback loop to iteratively
refine generated images toward a quality target.

## Overview

The pipeline accepts a natural language prompt and autonomously generates,
scores, and refines images until a candidate meeting all quality thresholds
is accepted. A tactical LLM interprets scorer feedback and decides whether
to accept a candidate, modify the prompt, adjust generation parameters, or
trigger targeted inpainting on specific regions.

All generation history — including rejected candidates — is stored in
LanceDB as vector embeddings alongside full scorer results and generation
parameters. This long-term memory enables the strategic LLM to identify
patterns across sessions and improve generation quality over time.

## Directory Structure

```
~/ai-image/
├── ARCHITECTURE.tex        LaTeX source for system architecture document
├── ARCHITECTURE.pdf        Rendered architecture document (make doc)
├── MESSAGES.md             Message schema contracts for all queues and exchanges
├── Makefile                Top-level build orchestration
├── lancedb_schema.py       LanceDB table schema definitions (Pydantic models)
├── lancedb_manager.py      LanceDB write manager (shared across loop and strategic LLM)
├── loop/                   Generation loop subsystem
│   ├── ComfyUI/            Stable Diffusion XL generation engine
│   ├── comfyui_worker.py   RabbitMQ consumer wrapping the ComfyUI API
│   ├── scorers/            Scorer processes, router, and aggregator
│   ├── start_broker.sh     Starts RabbitMQ and Redis
│   └── start_loop.sh       Starts all loop infrastructure
├── lancedb/                LanceDB persistent storage (sessions, loop tables)
└── strategic-llm/          Strategic LLM subsystem (planned)
```

## Prerequisites

### Hardware
- Apple Silicon Mac with at least 64GB unified memory (96GB recommended)
- ~100GB free disk space for models and generation output

### Software
- macOS 14 or later
- Homebrew
- Python 3.11+
- Rust (installed via rustup)
- MacTeX (for architecture documentation: `brew install --cask mactex`)

### Models
See loop/README.md for the full model download procedure.

## Quick Start

```bash
# Build Rust components
cd ~/ai-image
make build

# Start all infrastructure
~/ai-image/loop/start_loop.sh

# Submit a generation request
python -c "
import pika, json, uuid
conn = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
ch = conn.channel()
ch.queue_declare(queue='loop.request', durable=True)
ch.basic_publish(
    exchange='',
    routing_key='loop.request',
    body=json.dumps({
        'image_uuid': str(uuid.uuid4()),
        'session_uuid': str(uuid.uuid4()),
        'sequence_number': 1,
        'workflow_path': 'loop/ComfyUI/workflows/two_character_base.json',
        'prompt': 'two people in a park, photorealistic',
        'workflow_params': {
            'checkpoint': 'absolute_reality_xl.safetensors',
            'steps': 30, 'cfg': 7.0, 'sampler': 'dpm_2m_karras'
        }
    })
)
conn.close()
print('Submitted')
"
```

Monitor progress at http://localhost:15672 (guest/guest).

## Documentation

```bash
make doc        # renders ARCHITECTURE.pdf and all component docs
```

See ARCHITECTURE.pdf for full system design.
See MESSAGES.md for message schema contracts.
