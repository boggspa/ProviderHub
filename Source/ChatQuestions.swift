import SwiftUI

/// Drafts outlive lazy-row remounts and pane changes, and leave memory once settled.
@MainActor private final class QuestionDrafts: ObservableObject {
    static let shared = QuestionDrafts()
    @Published var answers: [String: [String: [String]]] = [:]
    @Published var custom: Set<String> = []
    @Published var text: [String: String] = [:]
    func clear(_ id: String) {
        answers.removeValue(forKey: id)
        custom = custom.filter { !$0.hasPrefix(id + ":") }
        text = text.filter { !$0.key.hasPrefix(id + ":") }
    }
}

@MainActor struct ChatQuestionCard: View {
    let question: ChatQuestionRecord
    let accent: Color
    let enabled: Bool
    var answer: ([String: [String]], Bool) -> Void
    @ObservedObject private var drafts = QuestionDrafts.shared
    @Environment(\.chatTextStyle) private var textStyle
    private var pending: Bool { question.status == "pending" }
    private var complete: Bool {
        question.questions.allSatisfy { !(drafts.answers[question.id]?[$0.id] ?? []).isEmpty }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "questionmark.bubble").foregroundStyle(accent)
                Text(question.asker + (pending ? " asks" : " asked")).font(textStyle.system(11.5, weight: .semibold))
                Spacer()
                Text(pending ? (question.mode == "wait" ? "Waiting for you" : "Answer when ready") : statusLabel)
                    .font(textStyle.system(10)).foregroundStyle(HubTheme.Semantic.secondaryInk)
            }
            ForEach(question.questions) { item in
                VStack(alignment: .leading, spacing: 8) {
                    if !item.header.isEmpty { Text(item.header).font(textStyle.system(10.5, weight: .semibold)).foregroundStyle(accent) }
                    Text(item.question).font(textStyle.system(13)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    if pending { controls(item) }
                    else if let values = question.answers[item.id], !values.isEmpty {
                        Label(values.joined(separator: "; "), systemImage: "checkmark")
                            .font(textStyle.system(12, weight: .medium)).foregroundStyle(accent).textSelection(.enabled)
                    }
                }
            }
            if pending {
                HStack {
                    Button("Dismiss") { answer([:], true) }.buttonStyle(.plain).foregroundStyle(HubTheme.Semantic.secondaryInk)
                    Spacer()
                    Button("Send answer") { answer(drafts.answers[question.id] ?? [:], false) }
                        .buttonStyle(.borderedProminent).tint(accent).disabled(!complete)
                }.font(textStyle.system(11.5)).disabled(!enabled)
            }
        }
        .padding(14).frame(maxWidth: 640, alignment: .leading)
        .hubCard(cornerRadius: HubTheme.Radius.panel, accent: accent)
        .onChange(of: question.status) { _, status in if status != "pending" { drafts.clear(question.id) } }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(question.asker + " question, " + (pending ? "pending" : statusLabel))
    }

    private var statusLabel: String {
        switch question.status {
        case "answered": return "Answered"
        case "dismissed": return "Dismissed · no answer sent"
        default: return "Cancelled · no answer sent"
        }
    }

    @ViewBuilder private func controls(_ item: ChatQuestionRecord.Item) -> some View {
        let key = question.id + ":" + item.id
        let custom = item.options.isEmpty || drafts.custom.contains(key)
        if !custom {
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
                            .foregroundStyle(selected ? accent : HubTheme.Semantic.secondaryInk)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(option.label).font(textStyle.system(12, weight: .medium))
                            if !option.description.isEmpty { Text(option.description).font(textStyle.system(11)).foregroundStyle(HubTheme.Semantic.secondaryInk) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.padding(8).background(selected ? accent.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).accessibilityLabel(option.label + (selected ? ", selected" : ""))
            }
            Button("Other…") { drafts.custom.insert(key); drafts.answers[question.id, default: [:]][item.id] = [] }
                .buttonStyle(.plain).font(textStyle.system(11.5)).foregroundStyle(accent)
        } else {
            TextField("Your answer…", text: Binding(get: { drafts.text[key] ?? "" }, set: { value in
                drafts.text[key] = value
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                drafts.answers[question.id, default: [:]][item.id] = trimmed.isEmpty ? [] : [trimmed]
            }), axis: .vertical).lineLimit(2...6).textFieldStyle(.plain).font(textStyle.system(12))
                .padding(10).background(HubTheme.Semantic.raisedSurface, in: RoundedRectangle(cornerRadius: 7))
                .accessibilityLabel("Your answer to " + item.question)
            if !item.options.isEmpty {
                Button("Back to options") { drafts.custom.remove(key); drafts.answers[question.id, default: [:]][item.id] = [] }
                    .buttonStyle(.plain).font(textStyle.system(11)).foregroundStyle(HubTheme.Semantic.secondaryInk)
            }
        }
    }
}
