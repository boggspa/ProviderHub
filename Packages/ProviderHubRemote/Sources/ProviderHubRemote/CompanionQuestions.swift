import SwiftUI

public struct RemoteQuestion: Codable, Equatable, Identifiable {
    public struct Option: Codable, Equatable, Identifiable { public var label: String; public var description: String; public var id: String { label } }
    public struct Item: Codable, Equatable, Identifiable {
        public var id: String; public var header: String; public var question: String
        public var options: [Option]; public var multiSelect: Bool
    }
    public var id: String; public var status: String; public var mode: String; public var asker: String
    public var questions: [Item]; public var answers: [String: [String]]
}

@MainActor private final class CompanionQuestionDrafts: ObservableObject {
    static let shared = CompanionQuestionDrafts()
    @Published var answers: [String: [String: [String]]] = [:]
    @Published var custom: Set<String> = []
    @Published var text: [String: String] = [:]
    func clear(_ id: String) {
        answers.removeValue(forKey: id)
        custom = custom.filter { !$0.hasPrefix(id + ":") }
        text = text.filter { !$0.key.hasPrefix(id + ":") }
    }
}

@MainActor struct CompanionQuestionCard: View {
    let question: RemoteQuestion
    let accent: Color
    let enabled: Bool
    var answer: ([String: [String]], Bool) -> Void
    @ObservedObject private var drafts = CompanionQuestionDrafts.shared
    private var pending: Bool { question.status == "pending" }
    private var complete: Bool { question.questions.allSatisfy { !(drafts.answers[question.id]?[$0.id] ?? []).isEmpty } }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "questionmark.bubble").foregroundStyle(accent)
                Text(question.asker + (pending ? " asks" : " asked")).font(.caption.weight(.semibold))
                Spacer(minLength: 0)
            }
            ForEach(question.questions) { item in
                VStack(alignment: .leading, spacing: 8) {
                    if !item.header.isEmpty { Text(item.header).font(.caption2.weight(.semibold)).foregroundStyle(accent) }
                    Text(item.question).font(.callout).textSelection(.enabled)
                    if pending { controls(item).disabled(!enabled) }
                    else if let values = question.answers[item.id], !values.isEmpty {
                        Label(values.joined(separator: "; "), systemImage: "checkmark").font(.callout.weight(.medium)).foregroundStyle(accent)
                    }
                }
            }
            if pending {
                HStack {
                    Button("Dismiss") { answer([:], true) }.buttonStyle(.plain).foregroundStyle(CompanionTheme.secondary)
                    Spacer()
                    Button("Send answer") { answer(drafts.answers[question.id] ?? [:], false) }
                        .buttonStyle(.borderedProminent).tint(accent).disabled(!complete)
                }.font(.caption).disabled(!enabled)
            }
            Text(pending ? (question.mode == "wait" ? "Waiting for you" : "Answer when ready") :
                    question.status == "answered" ? "Answered" : question.status == "dismissed" ? "Dismissed · no answer sent" : "Cancelled · no answer sent")
                .font(.caption2).foregroundStyle(CompanionTheme.secondary)
        }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .companionCard(cornerRadius: CompanionTheme.panelRadius, accent: accent)
            .onChange(of: question.status) { _, value in if value != "pending" { drafts.clear(question.id) } }
            .accessibilityElement(children: .contain)
    }
    @ViewBuilder private func controls(_ item: RemoteQuestion.Item) -> some View {
        let key = question.id + ":" + item.id
        if item.options.isEmpty || drafts.custom.contains(key) {
            TextField("Your answer…", text: Binding(get: { drafts.text[key] ?? "" }, set: { value in
                drafts.text[key] = value
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                drafts.answers[question.id, default: [:]][item.id] = trimmed.isEmpty ? [] : [trimmed]
            }), axis: .vertical).lineLimit(2...6).textFieldStyle(.plain).padding(10)
                .background(CompanionTheme.raised, in: RoundedRectangle(cornerRadius: 8)).accessibilityLabel("Your answer to " + item.question)
            if !item.options.isEmpty {
                Button("Back to options") { drafts.custom.remove(key); drafts.answers[question.id, default: [:]][item.id] = [] }.font(.caption)
            }
        } else {
            ForEach(item.options) { option in
                let selected = (drafts.answers[question.id]?[item.id] ?? []).contains(option.label)
                Button {
                    var values = drafts.answers[question.id]?[item.id] ?? []
                    if item.multiSelect {
                        if selected { values.removeAll { $0 == option.label } } else { values.append(option.label) }
                    } else { values = [option.label] }
                    drafts.answers[question.id, default: [:]][item.id] = values
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: item.multiSelect ? (selected ? "checkmark.square.fill" : "square") : (selected ? "largecircle.fill.circle" : "circle"))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(option.label).font(.callout.weight(.medium))
                            if !option.description.isEmpty { Text(option.description).font(.caption).foregroundStyle(CompanionTheme.secondary) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.padding(8).background(selected ? accent.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 8))
                }.buttonStyle(.plain).foregroundStyle(selected ? accent : CompanionTheme.ink)
                    .accessibilityLabel(option.label + (selected ? ", selected" : ""))
            }
            Button("Other…") { drafts.custom.insert(key); drafts.answers[question.id, default: [:]][item.id] = [] }.font(.caption).tint(accent)
        }
    }
}
