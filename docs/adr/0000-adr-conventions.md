# ADR 0000 — ADR Conventions and Lifecycle

## Status

Accepted

## Context

As the number of Architecture Decision Records grows, the project needs a consistent
set of conventions governing how ADRs are written, numbered, and how their statuses
evolve over time. Without explicit conventions, status labels become ambiguous and
cross-references between ADRs are inconsistent.

## Decision

### Numbering

- ADRs are numbered sequentially starting from `0000`.
- Numbers are zero-padded to four digits (e.g., `0001`, `0012`).
- Numbers are never reused, even if an ADR is deprecated or superseded.

### Status Lifecycle

Every ADR carries exactly one of the following statuses:

| Status                      | Meaning                                                |
|-----------------------------|--------------------------------------------------------|
| **Proposed**                | Under discussion; not yet approved for implementation. |
| **Accepted**                | Approved and active; guides current implementation.    |
| **Accepted (Transitional)** | Approved but explicitly temporary; will be replaced.   |
| **Superseded by ADR NNNN**  | No longer active; replaced by the referenced ADR.      |
| **Deprecated**              | No longer relevant; kept for historical context.       |

Transitions follow this graph:

```
Proposed → Accepted → Superseded by ADR NNNN
                    → Deprecated
Proposed → Accepted (Transitional) → Superseded by ADR NNNN
```

A status change must include the superseding ADR number where applicable.

### Structure

Every ADR must contain at minimum:

1. **Title** — `# ADR NNNN — Short Descriptive Title`
2. **Status** — one of the statuses above
3. **Context** — the problem or situation motivating the decision
4. **Decision** — what was decided and key design choices
5. **Consequences** — advantages, tradeoffs, and known limitations
6. **References** — cross-links to related ADRs and external resources

### Cross-References

- When an ADR depends on, extends, or supersedes another, it must include an
  explicit reference in both the body text and the `## References` section.
- Superseded ADRs must note their successor in the Status line.
- Relationship types: **Depends on**, **Extends**, **Supersedes**, **Constrained by**, **Related to**.

### Versioning

ADRs are **immutable once accepted**. Corrections or evolutions require a new ADR
that supersedes the original. Minor typographical fixes are permitted without a new ADR.

### Glossary

- **Agent** — the in-pod Python process that handles TCP/UDP relay.
- **Tunneling Service** — the multiplexing service that bridges the SOCKS5 gateway and the agent.
- **SOCKS5 Service** — the client-facing proxy entry point.
- **Session** — one Kubernetes exec attachment (may carry many connections).
- **Connection** — one TCP or UDP logical stream within a session.

## Consequences

- All existing and future ADRs follow a uniform structure.
- Status meanings are unambiguous across the project.
- Cross-references create a navigable decision graph.
- Consistent terminology reduces confusion.

## References

- This ADR is self-referential and establishes the baseline for all others.
