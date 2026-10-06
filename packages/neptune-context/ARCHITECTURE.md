# neptune-context architecture

```mermaid
flowchart LR
  LED[("Ledger catalog API 1.7.0")]
  MEM[("Memory graph schema 1.6.0")]
  subgraph P["neptune-context"]
    PIN["pins.py + contract suites on stubs"]
    QRY["query/ language + validation (ADR 0002)"]
    RET["retrieve/ channel interface + graph channel (ADR 0007)"]
    ENG["engine.py LocalEngine (ADR 0007)"]
    PKT["packets/"]
    RND["render/"]
    SDK["sdk/ Client + engine seam (ADR 0004)"]
    MCP["mcp/ 4 read-only tools (ADR 0004)"]
    EXP["explain/"]
    EVA["eval/"]
  end
  OUT[("Agents / Deploy / Learn")]
  CON[("contracts/query-packet")]
  LED -->|read only| RET
  MEM -->|read only| RET
  LED & MEM -.-> PIN
  QRY --> RET --> PKT --> RND
  RET --> EXP
  PKT --> SDK
  MCP --> SDK
  RET --> ENG -->|engine seam| SDK
  EVA -.-> RET
  PKT --> CON --> OUT
  SDK & MCP --> OUT

  subgraph KEY[" "]
    K1["built"]
    K2["partial"]
    K3["not built"]
  end
  OUT ~~~ K1 & K2 & K3

  classDef built fill:#6a9f7426,stroke:#6a9f74,stroke-width:2px
  classDef partial fill:#c09a5b26,stroke:#c09a5b,stroke-width:2px
  classDef todo fill:none,stroke:#8b949e,stroke-width:1.5px,stroke-dasharray:5 4
  classDef ext fill:none,stroke:#8b949e,stroke-width:1px
  class K1,PIN,PKT built
  class K2,QRY,RND,SDK,MCP,RET,ENG partial
  class EXP,EVA,K3 todo
  class LED,MEM,OUT,CON ext
  style P fill:#8b949e0f,stroke:#8b949e
  style KEY fill:none,stroke:none
  linkStyle default stroke:#8b949e
```
