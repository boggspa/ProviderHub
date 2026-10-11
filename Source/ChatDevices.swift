import AppKit
import SwiftUI

struct ChatDevicesSettings: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @AppStorage("chatRemoteHost") private var address = ""
    private var listening: Bool { model.remoteStatus["listening"] as? Bool ?? false }
    private var pending: [String: Any]? { model.remoteStatus["pendingPairing"] as? [String: Any] }
    private var devices: [[String: Any]] { model.remoteStatus["devices"] as? [[String: Any]] ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Devices").font(.headline); Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.cancelAction) }
            Form {
                Section("Remote access") {
                    TextField("Mac’s LAN or Tailscale IP", text: $address).disabled(listening)
                    HStack {
                        Text(model.remoteStatus["connected"] as? Bool == true ? "Phone connected" : listening ? "Waiting for a device" : "Off")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button(listening ? "Turn off" : "Enable") {
                            if listening { model.stopRemote() }
                            else { model.startRemote(host: address.trimmingCharacters(in: .whitespacesAndNewlines)) }
                        }.disabled(!model.connected || !listening && address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    Text("Your phone can view chats, send messages, stop work and answer pending approvals. Keep Provider Hub running on this Mac. Both devices need access to the same private network.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if listening {
                    Section("Pair a device") {
                        Text("Copy the pairing text into the iOS companion, then compare the code shown on both devices.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Copy pairing text") { copyBootstrap() }
                        if let pending, let identity = pending["identity"] as? String, let code = pending["confirmCode"] as? String {
                            Text(code).font(.system(.title2, design: .monospaced))
                            HStack {
                                Button("Codes match · Pair") { model.confirmDevice(identity: identity, code: code, allow: true) }
                                Button("Reject") { model.confirmDevice(identity: identity, code: code, allow: false) }
                            }
                        }
                    }
                }
                if !devices.isEmpty {
                    Section("Paired devices") {
                        ForEach(Array(devices.enumerated()), id: \.offset) { _, device in
                            if device["revoked"] as? Bool != true, let identity = device["identity"] as? String {
                                HStack {
                                    Text(device["label"] as? String ?? "iPhone")
                                    Spacer()
                                    Button("Remove") { model.revokeDevice(identity: identity) }
                                }
                            }
                        }
                    }
                }
            }.formStyle(.grouped)
            if !model.remoteNotice.isEmpty { Text(model.remoteNotice).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
            if let error = model.remoteStatus["error"] as? String, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }.padding(20).frame(width: 480, height: 530)
            .task {
                while !Task.isCancelled {
                    model.refreshDevices()
                    do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                }
            }
    }
    private func copyBootstrap() {
        guard let bootstrap = model.remoteStatus["bootstrap"] as? [String: Any],
              let data = try? JSONSerialization.data(withJSONObject: bootstrap, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
