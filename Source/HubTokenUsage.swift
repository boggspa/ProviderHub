import Foundation
import CoreFoundation

/// Provider-reported usage only. Missing cache fields mean unavailable, not
/// zero hits, and cache totals describe input reuse rather than money/quotas.
struct HubTokenUsage {
    var input: Int?
    var output: Int?
    var cacheRead: Int?
    var cacheWrite: Int?
    var thinking: Int?
    var write5m: Int?
    var write1h: Int?

    init(_ raw: [String: Any]) {
        func count(_ value: Any?) -> Int? {
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(),
                  let integer = value as? Int, integer >= 0 else { return nil }
            return integer
        }
        input = count(raw["input_tokens"]); output = count(raw["output_tokens"])
        cacheRead = count(raw["cache_read_input_tokens"])
        cacheWrite = count(raw["cache_creation_input_tokens"])
        thinking = count((raw["output_tokens_details"] as? [String: Any])?["thinking_tokens"])
        let writes = raw["cache_creation"] as? [String: Any]
        write5m = count(writes?["ephemeral_5m_input_tokens"])
        write1h = count(writes?["ephemeral_1h_input_tokens"])
    }

    var totalInput: Int? {
        guard let input, let cacheRead, let cacheWrite else { return nil }
        let first = input.addingReportingOverflow(cacheRead)
        let total = first.partialValue.addingReportingOverflow(cacheWrite)
        return first.overflow || total.overflow ? nil : total.partialValue
    }
    var cacheReadPercent: Double? {
        guard let totalInput, totalInput > 0, let cacheRead else { return nil }
        return Double(cacheRead) / Double(totalInput) * 100
    }
    var summary: String {
        var fields: [String] = []
        if let cacheRead {
            var read = "Cache read \(cacheRead.formatted())"
            if let percent = cacheReadPercent { read += String(format: " (%.1f%% of input)", percent) }
            fields.append(read)
        }
        if let cacheWrite { fields.append("Cache write \(cacheWrite.formatted())") }
        if cacheRead == nil && cacheWrite == nil { fields.append("Cache telemetry unavailable") }
        if let input { fields.append("\(cacheRead != nil || cacheWrite != nil ? "Uncached input" : "Input") \(input.formatted())") }
        if let output { fields.append("Output \(output.formatted())") }
        if let thinking { fields.append("Thinking \(thinking.formatted()) (included in output)") }
        if let write5m { fields.append("5m write \(write5m.formatted())") }
        if let write1h { fields.append("1h write \(write1h.formatted())") }
        return fields.joined(separator: " · ")
    }
}
