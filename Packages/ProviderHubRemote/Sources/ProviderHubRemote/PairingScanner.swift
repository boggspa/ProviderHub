#if os(iOS)
import AVFoundation
import SwiftUI
import UIKit

/// Camera use begins only while this explicitly opened sheet is visible.
@MainActor
struct PairingScannerView: View {
    var onCode: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var notice = "Point the camera at the QR code in Provider Hub’s Devices settings."
    @State private var permissionDenied = false
    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                PairingCameraView(onCode: onCode, onStatus: { message, denied in
                    notice = message; permissionDenied = denied
                })
                .background(.black).clipShape(RoundedRectangle(cornerRadius: 16))
                .accessibilityLabel("Pairing QR scanner")
                Text(notice).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                if permissionDenied {
                    Button("Open camera settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                }
                Text("After scanning, compare the code on both devices and approve pairing on your Mac.")
                    .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }.padding()
                .navigationTitle("Scan pairing code")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

@MainActor
private struct PairingCameraView: UIViewControllerRepresentable {
    var onCode: (String) -> Void
    var onStatus: (String, Bool) -> Void
    func makeUIViewController(context: Context) -> PairingCameraController {
        PairingCameraController(onCode: onCode, onStatus: onStatus)
    }
    func updateUIViewController(_ controller: PairingCameraController, context: Context) {}
    static func dismantleUIViewController(_ controller: PairingCameraController, coordinator: ()) { controller.stop() }
}

/// Serializes capture configuration/start/stop off the UI thread. The session
/// is attached to a preview layer, but only this queue mutates capture state.
private final class PairingCameraSession: @unchecked Sendable {
    let capture = AVCaptureSession()
    private let queue = DispatchQueue(label: "providerhub.pairing-camera")
    private var active = false
    func start(delegate: any AVCaptureMetadataOutputObjectsDelegate, status: @escaping @Sendable (String) -> Void) {
        queue.async { [self] in
            do {
                if capture.inputs.isEmpty {
                    guard let camera = AVCaptureDevice.default(for: .video) else {
                        status("No camera is available. You can still paste pairing text from your Mac."); return
                    }
                    let input = try AVCaptureDeviceInput(device: camera)
                    let output = AVCaptureMetadataOutput()
                    capture.beginConfiguration()
                    defer { capture.commitConfiguration() }
                    guard capture.canAddInput(input), capture.canAddOutput(output) else {
                        status("The camera could not start. You can still paste pairing text."); return
                    }
                    capture.addInput(input); capture.addOutput(output)
                    guard output.availableMetadataObjectTypes.contains(.qr) else {
                        capture.removeInput(input); capture.removeOutput(output)
                        status("QR scanning is unavailable on this camera."); return
                    }
                    output.setMetadataObjectsDelegate(delegate, queue: .main)
                    output.metadataObjectTypes = [.qr]
                }
                active = true
            } catch { status("The camera could not start: \(error.localizedDescription)"); return }
            if active && !capture.isRunning { capture.startRunning() }
        }
    }
    func stop() {
        queue.async { [self] in
            active = false
            if capture.isRunning { capture.stopRunning() }
        }
    }
}

@MainActor
private final class PairingCameraController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    private let camera = PairingCameraSession()
    private var preview: AVCaptureVideoPreviewLayer?
    private var visible = false
    private var delivered = false
    private let onCode: (String) -> Void
    private let onStatus: (String, Bool) -> Void

    init(onCode: @escaping (String) -> Void, onStatus: @escaping (String, Bool) -> Void) {
        self.onCode = onCode; self.onStatus = onStatus
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is unavailable") }
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .black
        let layer = AVCaptureVideoPreviewLayer(session: camera.capture)
        layer.videoGravity = .resizeAspectFill
        view.layer.addSublayer(layer); preview = layer
        NotificationCenter.default.addObserver(self, selector: #selector(backgrounded), name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(foregrounded), name: UIApplication.willEnterForegroundNotification, object: nil)
    }
    deinit { NotificationCenter.default.removeObserver(self) }
    @objc private func backgrounded() { camera.stop() }
    @objc private func foregrounded() {
        if visible, AVCaptureDevice.authorizationStatus(for: .video) == .authorized { beginCapture() }
    }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated); visible = true
        guard AVCaptureDevice.default(for: .video) != nil else {
            onStatus("No camera is available. You can still paste pairing text from your Mac.", false)
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: beginCapture()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                Task { @MainActor [weak self] in
                    guard let self, self.visible else { return }
                    if granted { self.beginCapture() }
                    else { self.onStatus("Camera access is off. Enable it in Settings or paste pairing text.", true) }
                }
            }
        case .denied, .restricted:
            onStatus("Camera access is off. Enable it in Settings or paste pairing text.", true)
        @unknown default: onStatus("Camera access is unavailable. You can still paste pairing text.", false)
        }
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); preview?.frame = view.bounds }
    override func viewDidDisappear(_ animated: Bool) { super.viewDidDisappear(animated); stop() }
    func stop() { visible = false; camera.stop() }
    private func beginCapture() {
        guard visible, !delivered else { return }
        camera.start(delegate: self) { [weak self] message in
            Task { @MainActor [weak self] in self?.onStatus(message, false) }
        }
    }
    nonisolated func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        let text = metadataObjects.compactMap { ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue }.first
        guard let text else { return }
        Task { @MainActor [weak self] in
            guard let self, self.visible, !self.delivered else { return }
            do { _ = try RemoteBootstrap.decodePairingText(text) }
            catch { self.onStatus(error.localizedDescription, false); return }
            self.delivered = true; self.camera.stop(); self.onCode(text)
        }
    }
}
#endif
