import AppKit
import Darwin
import SwiftUI

@MainActor
struct ChatDevicesSettings: View {
    @ObservedObject var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @AppStorage("chatRemoteHost") private var address = ""
    private var listening: Bool { model.remoteStatus["listening"] as? Bool ?? false }
    private var pending: [String: Any]? { model.remoteStatus["pendingPairing"] as? [String: Any] }
    private var devices: [[String: Any]] { model.remoteStatus["devices"] as? [[String: Any]] ?? [] }
    @State private var pairingImage: CGImage?
    private var pairingText: String? {
        (model.remoteStatus["bootstrap"] as? [String: Any]).flatMap { ChatPairingCode.payload($0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Devices").font(.headline); Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.cancelAction) }
            Form {
                Section("Remote access") {
                    TextField("Mac’s LAN or Tailscale IP", text: $address).disabled(listening)
                    if !listening, let tailscale = tailscaleAddress {
                        Button("Use Tailscale · \(tailscale)") { address = tailscale }
                    }
                    HStack {
                        Text(model.remoteStatus["connected"] as? Bool == true ? "Phone connected" : listening ? "Waiting for a device" : "Off")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button(listening ? "Turn off" : "Enable") {
                            if listening { model.stopRemote() }
                            else { model.startRemote(host: address.trimmingCharacters(in: .whitespacesAndNewlines)) }
                        }.disabled(!model.connected || !listening && address.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    Text("Your phone can view chats, send messages, stop work and answer pending approvals. Keep Provider Hub running on this Mac. For access over cellular or another Wi-Fi network, connect both devices to the same Tailscale tailnet and use this Mac’s Tailscale IP. Allow TCP port 8765 in your tailnet policy.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if listening {
                    Section("Pair a device") {
                        HStack(alignment: .center, spacing: 16) {
                            if let pairingImage {
                                Image(decorative: pairingImage, scale: 1).resizable().interpolation(.none)
                                    .frame(width: 168, height: 168)
                                    .accessibilityLabel("Pairing QR code")
                            }
                            VStack(alignment: .leading, spacing: 10) {
                                Text("Choose Scan QR code in the iOS companion, then compare the code shown on both devices.")
                                    .font(.caption).foregroundStyle(.secondary)
                                Button("Copy pairing text") { copyBootstrap() }
                                Text("For Tailscale, this QR code must contain the Mac’s Tailscale IP. You can also copy the pairing text to your phone.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
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
        }.padding(20).frame(width: 520, height: 620)
            .task(id: pairingText) { pairingImage = pairingText.flatMap { ChatPairingCode.image(for: $0) } }
            .task {
                while !Task.isCancelled {
                    model.refreshDevices()
                    do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                }
            }
    }
    private func copyBootstrap() {
        guard let text = pairingText else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private var tailscaleAddress: String? {
        var interfaces: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&interfaces) == 0 else { return nil }
        defer { freeifaddrs(interfaces) }
        var cursor = interfaces
        while let interface = cursor {
            defer { cursor = interface.pointee.ifa_next }
            guard let socketAddress = interface.pointee.ifa_addr,
                  socketAddress.pointee.sa_family == UInt8(AF_INET) else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(socketAddress, socklen_t(socketAddress.pointee.sa_len),
                              &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
            let address = String(cString: host)
            let bytes = address.split(separator: ".").compactMap { Int($0) }
            if bytes.count == 4, bytes[0] == 100, (64...127).contains(bytes[1]) { return address }
        }
        return nil
    }
}
