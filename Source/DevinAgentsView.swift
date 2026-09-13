import SwiftUI

struct DevinAgentsPage: View {
    @ObservedObject var model: BridgeModel
    @State private var showCreate = false

    private var devinConfigured: Bool {
        model.providerDefinitions.first { $0.id == "devin" } != nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if !devinConfigured {
                Panel {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Devin is not available yet").font(.headline)
                        Text("Add the Devin provider in Providers, then return here to create agent sessions.").font(.caption).foregroundStyle(.secondary)
                        Button("Open Providers") { model.page = .connection }
                    }
                }
            } else {
                Panel {
                    HStack {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("Devin agent sessions").font(.headline)
                            Text("Session-based AI agent · normal, fast, lite, ultra, fusion").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Refresh") { Task { await model.devinLoadSessions(); await model.devinFetchCatalogue() } }.disabled(model.busy || model.devinLoading)
                    }
                    if !model.devinError.isEmpty {
                        Text(model.devinError).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    }
                    HStack {
                        Picker("Mode", selection: $model.devinSessionMode) {
                            ForEach(model.devinModes) { mode in Text(mode.title).tag(mode.id) }
                        }.frame(maxWidth: 200)
                        TextField("What should Devin do?", text: $model.devinSessionTask).textFieldStyle(.roundedBorder)
                        Button("Create session") { Task { await model.devinCreateSession() } }.disabled(model.busy || model.devinLoading || model.devinSessionTask.isEmpty)
                    }
                    if !model.devinNotice.isEmpty {
                        Text(model.devinNotice).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Panel {
                    HStack {
                        Text("Sessions").font(.headline)
                        Spacer()
                        Text("\\(model.devinSessions.count) sessions").font(.caption).foregroundStyle(.secondary)
                    }
                    if model.devinLoading { ProgressView().controlSize(.small) }
                    if model.devinSessions.isEmpty && !model.devinLoading {
                        Text("No sessions yet. Create one above to get started.").font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(model.devinSessions) { session in
                        DevinSessionRow(session: session, model: model)
                    }
                }
            }
        }
        .onAppear {
            Task { await model.devinFetchCatalogue(); await model.devinLoadSessions() }
        }
    }
}

struct DevinSessionRow: View {
    let session: DevinSessionSummary
    @ObservedObject var model: BridgeModel

    private var stateColor: Color {
        switch session.state {
        case "completed": return .green
        case "failed": return .orange
        case "running", "pending": return .blue
        default: return .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Circle().fill(stateColor).frame(width: 8, height: 8).padding(.top, 5)
            VStack(alignment: .leading, spacing: 4) {
                Text(session.displayTitle).font(.system(size: 13, weight: .medium)).lineLimit(2)
                HStack(spacing: 8) {
                    if let mode = session.mode { Text(mode).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary) }
                    Text(session.state ?? "unknown").font(.system(size: 10)).foregroundStyle(.secondary)
                    if let createdAt = session.created_at { Text(createdAt).font(.system(size: 10)).foregroundStyle(.tertiary) }
                }
                if let error = session.error, !error.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.orange).lineLimit(2)
                }
            }
            Spacer()
            if session.isActive {
                Button("Cancel") { Task { await model.devinCancel(session) } }.buttonStyle(.bordered).controlSize(.small)
            } else {
                Button("Archive") { Task { await model.devinArchive(session) } }.buttonStyle(.bordered).controlSize(.small)
            }
        }
        .padding(.vertical, 4)
        if session.id != model.devinSessions.last?.id { Divider() }
    }
}
