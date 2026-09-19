import Foundation

struct MappingOptions: Codable, Equatable {
    var omit_system = false
    var omit_tools = false
    var compact_limit: Int?
}

/// One curated Claude catalogue row: a provider route served to Claude Desktop
/// under a family tier (see hub_config.claude_catalogue_rows for the served id).
struct ClaudeCatalogueEntry: Codable, Equatable, Identifiable {
    var route: String
    var tier: String
    var tier_default: Bool? = false
    var compact_limit: Int?
    var id: String { route }
}

/// Derive a catalogue for legacy settings without changing the saved mappings.
/// A route can appear only once in a curated catalogue, so its first slot owns
/// its tier and the smallest explicit threshold keeps compaction conservative.
func seedClaudeCatalogue(mappings: [String: String], options: [String: MappingOptions]) -> [ClaudeCatalogueEntry] {
    let legacySlots: [(id: String, tier: String)] = [
        ("claude-fable-5", "fable"),
        ("claude-opus-5", "opus"),
        ("claude-sonnet-5", "sonnet"),
        ("claude-haiku-4-5", "haiku"),
        ("claude-sonnet-4-6", "sonnet"),
    ]
    var entries: [ClaudeCatalogueEntry] = []
    var routeIndices: [String: Int] = [:]
    var defaultTiers: Set<String> = []
    for slot in legacySlots {
        guard let route = mappings[slot.id], !route.isEmpty else { continue }
        let threshold = options[slot.id]?.compact_limit
        if let index = routeIndices[route] {
            if let threshold = threshold {
                entries[index].compact_limit = min(entries[index].compact_limit ?? threshold, threshold)
            }
            continue
        }
        routeIndices[route] = entries.count
        entries.append(ClaudeCatalogueEntry(
            route: route,
            tier: slot.tier,
            tier_default: defaultTiers.insert(slot.tier).inserted,
            compact_limit: threshold
        ))
    }
    return entries
}
