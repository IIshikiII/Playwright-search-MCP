# Qwen Code - Work Pipeline

## Mandatory Pipeline for Complex Tasks or Uncertainty

When faced with complex tasks, uncertainty, or when additional context is needed, you **MUST** follow this pipeline **before writing any code**:

1. **Search via MCP Web Search** - Use `mcp__WebSearch__Web_search` to search for information
2. **Fetch Documentation/Forums** - Use the built-in `web_fetch` tool to browse documentation and forums
3. **Write Code** - Only after gathering all necessary information

### Why this pipeline?

- Ensures you have up-to-date and accurate information
- Reduces errors from assumptions or outdated knowledge
- Leverages external resources before making decisions

### When to use this pipeline

- You're unsure about the correct approach
- Working with unfamiliar libraries, frameworks, or APIs
- Debugging complex issues
- Need to verify current best practices
- Task requirements are ambiguous

---
