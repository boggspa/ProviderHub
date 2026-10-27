import Foundation

/// The Team editor and its Apply button use the same validation result.
enum CompanionTeamValidation {
    static let maxMembers = 12
    static let contextPresets = [16000, 32000, 64000, 120000, 128000, 200000, 325000, 500000, 650000, 1000000, 2000000]
    // Match ChatTeamExecution.maxProcesses and the host's MAX_PROCESSES.
    static let processRange = 1...16

    static func message(rows: [RemoteValue], context: String, processes: String,
                        minutes: String, tokens: String) -> String? {
        guard !rows.isEmpty else { return "Add at least one Team member." }
        guard rows.count <= maxMembers else { return "A Team can have at most \(maxMembers) members." }
        let names = rows.map { $0["name"].text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
        guard names.allSatisfy({ !$0.isEmpty }) else { return "Give each member a name." }
        guard rows.allSatisfy({ !$0["choice"].text.isEmpty }) else { return "Choose a model for each member." }
        guard Set(names).count == rows.count else { return "Use a different name for each member." }
        guard (16000...2000000).contains(Int(context) ?? 0) else { return "Context tokens must be between 16,000 and 2,000,000." }
        guard processRange.contains(Int(processes) ?? 0) else { return "Concurrent processes must be between 1 and 16." }
        guard minutes.isEmpty || (1...10080).contains(Int(minutes) ?? 0) else { return "Minutes must be between 1 and 10,080, or left empty." }
        guard tokens.isEmpty || (1...2000000000).contains(Int(tokens) ?? 0) else { return "Token limit must be between 1 and 2,000,000,000, or left empty." }
        return nil
    }
}
