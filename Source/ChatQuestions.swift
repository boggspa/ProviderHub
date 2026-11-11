import SwiftUI

/// Drafts outlive lazy-row remounts and pane changes, and leave memory once settled.
@MainActor final class QuestionDrafts: ObservableObject {
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

// MARK: - Pinned question stack

/// A question still waiting for an answer, read from the transcript row that
/// carries it. The row stays the record; this is the working copy the stack
/// shows above the composer.
struct ChatPendingQuestion: Identifiable, Equatable {
    /// The question's own id: what an answer is delivered against.
    var id: String { record.id }
    /// The transcript row the question was asked in.
    var entryID: String
    /// The team member or model that asked.
    var asker: String
    /// The route the asker runs on, for its accent.
    var route: String
    /// The team member that asked, when a Team member did.
    var memberID: String?
    /// The record: the options, the mode and the status.
    var record: ChatQuestionRecord

    init(entryID: String, route: String, memberID: String? = nil, record: ChatQuestionRecord) {
        self.entryID = entryID
        self.route = route
        self.memberID = memberID
        self.record = record
        self.asker = record.asker
    }

    /// Identity is the question, not the row it happens to sit in.
    static func == (lhs: ChatPendingQuestion, rhs: ChatPendingQuestion) -> Bool {
        lhs.id == rhs.id && lhs.entryID == rhs.entryID
    }

    /// How many things this card asks.
    var itemCount: Int { record.questions.count }
    /// "1 question" / "2 questions".
    var itemLabel: String { itemCount == 1 ? "1 question" : "\(itemCount) questions" }
    /// An async question is answered while the agent keeps working.
    var isAsync: Bool { record.mode == "async" }
}

/// The stack's memory: which question is on top, and the list it was on, so a
/// card that has just been answered hands over to the question that took its
/// place instead of jumping back to the oldest.
struct ChatQuestionQueue: Equatable {
    /// The question on top, by id.
    private(set) var visibleID: String?
    /// The pending list as it was last read, for handing over.
    private(set) var previous: [ChatPendingQuestion] = []

    init(visibleID: String? = nil, previous: [ChatPendingQuestion] = []) {
        self.visibleID = visibleID
        self.previous = previous
    }

    /// Fold a fresh list of pending questions in: the card on top stays while
    /// it is still open, and the question that replaced it takes over when it
    /// has just been answered or dismissed.
    mutating func update(_ next: [ChatPendingQuestion]) {
        visibleID = Self.resolve(shown: visibleID, previous: previous, pending: next)?.id
        previous = next
    }

    /// The card to show for `pending`, given the card that was showing.
    static func resolve(shown: String?, previous: [ChatPendingQuestion],
                        pending: [ChatPendingQuestion]) -> ChatPendingQuestion? {
        guard !pending.isEmpty else { return nil }
        if let shown, let match = pending.first(where: { $0.id == shown }) { return match }
        if let shown, let index = previous.firstIndex(where: { $0.id == shown }) {
            return pending[min(index, pending.count - 1)]
        }
        return pending.first
    }

    /// The card on top of `pending`.
    func visible(in pending: [ChatPendingQuestion]) -> ChatPendingQuestion? {
        Self.resolve(shown: visibleID, previous: previous, pending: pending)
    }

    /// Show one question of the queue.
    mutating func select(_ id: String, in pending: [ChatPendingQuestion]) {
        guard pending.contains(where: { $0.id == id }) else { return }
        visibleID = id
    }

    /// Move along the queue, and stay put at either end.
    mutating func step(_ delta: Int, in pending: [ChatPendingQuestion]) {
        guard let card = visible(in: pending),
              let index = pending.firstIndex(where: { $0.id == card.id }) else { return }
        let target = index + delta
        guard pending.indices.contains(target) else { return }
        visibleID = pending[target].id
    }

    /// "2 of 3", for the queue header.
    static func position(index: Int, count: Int) -> String {
        count == 0 ? "" : "\(min(max(index, 0), count - 1) + 1) of \(count)"
    }
}

/// The pending questions, pinned above the composer: one card at a time with
/// the rest of the queue behind it. A question asked in the middle of a long
/// transcript no longer scrolls out of reach, and only one active card is on
/// screen at a time. Every question keeps its transcript row, which becomes
/// the record once it is answered.
@MainActor struct ChatPinnedQuestionStack: View {
    /// A card can bundle several questions and their options. Capping it
    /// keeps the composer and the transcript where they are, rather than
    /// letting one long card push the composer off the pane.
    static let maxCardHeight: CGFloat = 280

    /// The questions still waiting, oldest first.
    let questions: [ChatPendingQuestion]
    /// The accent of the asker, for the card and the queue header.
    let accent: (ChatPendingQuestion) -> Color
    /// Whether an answer can be sent right now.
    let enabled: Bool
    /// The question, the answers per question id, and whether the card was
    /// dismissed rather than answered.
    let onAnswer: (ChatPendingQuestion, [String: [String]], Bool) -> Void

    @State private var queue = ChatQuestionQueue()
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        Group {
            if let card = queue.visible(in: questions) {
                VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
                    header(card)
                    if otherAskers.isEmpty == false {
                        Text("Also waiting: " + otherAskers.joined(separator: ", "))
                            .font(textStyle.system(10.5)).foregroundStyle(HubTheme.Semantic.secondaryInk)
                    }
                    ScrollView {
                        ChatQuestionCard(question: card.record, accent: accent(card), enabled: enabled) { answers, dismiss in
                            onAnswer(card, answers, dismiss)
                        }
                    }
                    .frame(maxHeight: Self.maxCardHeight)
                }
                .padding(.horizontal, 20).padding(.vertical, textStyle.scaled(6))
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .onChange(of: questions.map(\.id)) { _, _ in
            // A question that has left the queue has been answered or
            // dismissed: its draft goes with it, so the next answer does not
            // start from the last one.
            for gone in queue.previous where questions.contains(where: { $0.id == gone.id }) == false {
                QuestionDrafts.shared.clear(gone.id)
            }
            queue.update(questions)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Pending question")
    }

    /// The queue line: how many questions are waiting, which one is on top,
    /// and the controls to walk through the rest.
    private func header(_ card: ChatPendingQuestion) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "questionmark.bubble.fill")
                .font(textStyle.system(11, weight: .semibold)).foregroundStyle(accent(card))
            Text(questions.count == 1 ? "1 question waiting" : "\(questions.count) questions waiting")
                .font(textStyle.system(11.5, weight: .semibold)).foregroundStyle(HubTheme.Semantic.ink)
            Spacer(minLength: 8)
            if questions.count > 1 {
                Text(position)
                    .font(textStyle.system(11)).foregroundStyle(HubTheme.Semantic.secondaryInk)
                Button { queue.step(-1, in: questions) } label: {
                    Image(systemName: "chevron.left").font(textStyle.system(10, weight: .semibold))
                }
                .buttonStyle(.plain).foregroundStyle(HubTheme.Semantic.secondaryInk)
                .disabled(canStep(-1) == false).help("Previous question")
                Button { queue.step(1, in: questions) } label: {
                    Image(systemName: "chevron.right").font(textStyle.system(10, weight: .semibold))
                }
                .buttonStyle(.plain).foregroundStyle(HubTheme.Semantic.secondaryInk)
                .disabled(canStep(1) == false).help("Next question")
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Pending questions: " + position)
    }

    /// The other askers still waiting, so a Team's questions read as a queue
    /// rather than as one card at a time.
    private var otherAskers: [String] {
        guard let card = queue.visible(in: questions) else { return [] }
        var names: [String] = []
        for question in questions where question.id != card.id {
            if question.asker.isEmpty == false, names.contains(question.asker) == false {
                names.append(question.asker)
            }
        }
        return names
    }

    /// Where the card on top sits in the queue.
    private var visibleIndex: Int? {
        queue.visible(in: questions).flatMap { card in questions.firstIndex { $0.id == card.id } }
    }

    /// "2 of 3".
    private var position: String {
        ChatQuestionQueue.position(index: visibleIndex ?? 0, count: questions.count)
    }

    /// Whether a question sits one step away in the queue.
    private func canStep(_ delta: Int) -> Bool {
        guard let index = visibleIndex else { return false }
        return questions.indices.contains(index + delta)
    }
}

/// The pinned stack for a chat: it reads the pending questions from the
/// model, answers through the model, and takes the accent from the asker's
/// route. The window places it above the activity strip, above the composer.
@MainActor struct ChatPinnedQuestions: View {
    @ObservedObject var model: ChatModel

    var body: some View {
        ChatPinnedQuestionStack(
            questions: model.pendingQuestions(),
            accent: { model.accent(for: $0.route) },
            enabled: model.connected,
            onAnswer: { card, answers, dismiss in
                model.answerQuestion(card.record, answers: answers, dismiss: dismiss)
            }
        )
    }
}

extension ChatModel {
    /// The questions still waiting for an answer, in the order they were
    /// asked. The transcript keeps every question as a record; this is the
    /// working queue the stack above the composer shows.
    func pendingQuestions() -> [ChatPendingQuestion] {
        entries.compactMap { entry in
            guard entry.kind == "question", let record = entry.question, record.status == "pending" else { return nil }
            return ChatPendingQuestion(entryID: entry.id, route: entry.route, memberID: entry.memberID, record: record)
        }
    }
}
