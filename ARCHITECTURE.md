# Neptune architecture

```mermaid
flowchart LR
  RAW["Raw robotics evidence<br/>logs · bags · URDF<br/>docs · images"]

  subgraph N["Neptune"]
    DISC["Discovery &amp; identity"]
    RT["Ingestion runtime"]
    AD["Format adapters"]
    CAN["Canonical model<br/>records + provenance"]
    PKG[("Ingest package")]
    VAL["Validation &amp; alignment"]
    DER["Derived annotations"]
  end

  subgraph D["Downstream, not Neptune"]
    MEM["World memory"]
    RET["Retrieval / context"]
    USE["Agents · VLAs · sim · evals"]
  end

  RAW --> DISC --> RT
  RT <-->|chunks / records| AD
  AD -.->|conforms to| CAN
  RT --> PKG
  PKG <--> VAL
  PKG --> DER
  PKG ==> MEM --> RET --> USE
  DER -.-> MEM

  subgraph KEY[" "]
    K1["built"]
    K2["partial"]
    K3["not built"]
  end
  USE ~~~ K1 & K2 & K3

  classDef built fill:#6a9f7426,stroke:#6a9f74,stroke-width:2px
  classDef partial fill:#c09a5b26,stroke:#c09a5b,stroke-width:2px
  classDef todo fill:none,stroke:#8b949e,stroke-width:1.5px,stroke-dasharray:5 4
  classDef ext fill:none,stroke:#8b949e,stroke-width:1px
  class CAN,K1 built
  class DISC,PKG,K2 partial
  class RT,AD,VAL,DER,K3 todo
  class RAW,MEM,RET,USE ext
  style N fill:#8b949e0f,stroke:#8b949e
  style D fill:none,stroke:#8b949e,stroke-dasharray:2 3
  style KEY fill:none,stroke:none
  linkStyle default stroke:#8b949e
```
