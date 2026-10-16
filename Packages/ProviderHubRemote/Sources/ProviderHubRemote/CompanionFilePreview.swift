import SwiftUI
#if os(iOS)
import UIKit
import QuickLook
#else
import AppKit
#endif

extension CompanionModel {
    @MainActor
    public func fetchAttachment(_ file: RemoteValue, chat: String) async throws -> URL {
        let id = file["id"].text
        let key = chat + "/" + id
        if let cached = downloadedFiles[key], FileManager.default.fileExists(atPath: cached.path) { return cached }
        guard !fileDownloads.contains(key) else { throw RemoteControlFailure.rejected("This file is already downloading") }
        while fileDownloads.count >= 2 { try await Task.sleep(nanoseconds: 100_000_000); try Task.checkCancellation() }
        fileDownloads.insert(key)
        defer { fileDownloads.remove(key) }
        var offset = 0
        var revision = ""
        var bytes = Data()
        while true {
            try Task.checkCancellation()
            var args: [String: RemoteValue] = ["attachment": .string(id), "offset": .number(Double(offset))]
            if !revision.isEmpty { args["revision"] = .string(revision) }
            let part = try await performControl("remote_attachment_read", args, chat: chat)
            guard part["offset"].int == offset, let data = Data(base64Encoded: part["data"].text),
                  part["size"].int <= 64 * 1024 * 1024, bytes.count + data.count <= 64 * 1024 * 1024,
                  part["nextOffset"].int == offset + data.count else { throw RemoteControlFailure.rejected("Invalid attachment response") }
            revision = part["revision"].text
            bytes.append(data); offset += data.count
            if part["done"].flag { guard offset == part["size"].int else { throw RemoteControlFailure.rejected("Incomplete download") }; break }
            guard !data.isEmpty else { throw RemoteControlFailure.rejected("Attachment download stopped") }
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("ProviderHubPreviews").appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let name = (file["name"].text as NSString).lastPathComponent
        let url = folder.appendingPathComponent(name.isEmpty || name == "." || name == ".." ? "attachment" : name)
        try bytes.write(to: url, options: .atomic)
        downloadedFiles[key] = url
        return url
    }

    @MainActor
    func fetchThumbnail(_ file: RemoteValue, chat: String) async throws -> Data {
        let key = chat + "/" + file["id"].text
        if let cached = attachmentThumbnails[key] { return cached }
        let result = try await performControl("remote_attachment_thumbnail", ["attachment": file["id"]], chat: chat)
        guard let image = Data(base64Encoded: result["data"].text), image.count <= 100000 else { throw RemoteControlFailure.rejected("Invalid thumbnail") }
        attachmentThumbnails[key] = image
        return image
    }
}

struct CompanionFileTile: View {
    @ObservedObject var model: CompanionModel
    var file: RemoteValue
    var chat: String
    @State private var url: URL?
    @State private var loading = false
    @State private var notice = ""
    @State private var previewing = false
    @State private var thumbnail: Data?
    var body: some View {
        Button { Task { @MainActor in await load(open: true) } } label: {
            VStack(alignment: .leading, spacing: 3) {
                ZStack {
                    RoundedRectangle(cornerRadius: 5).fill(CompanionTheme.raised)
                    #if os(iOS)
                    if let thumbnail, let image = UIImage(data: thumbnail) {
                        Image(uiImage: image).resizable().scaledToFill().frame(width: 108, height: 64).clipped()
                    } else { Image(systemName: glyph).foregroundStyle(CompanionTheme.secondary) }
                    #else
                    if let thumbnail, let image = NSImage(data: thumbnail) {
                        Image(nsImage: image).resizable().scaledToFill().frame(width: 108, height: 64).clipped()
                    } else { Image(systemName: glyph).foregroundStyle(CompanionTheme.secondary) }
                    #endif
                    if loading { ProgressView().controlSize(.small) }
                }.frame(width: 108, height: 64).clipShape(RoundedRectangle(cornerRadius: 5))
                Text(file["name"].text).font(.system(size: 10.5)).lineLimit(1).truncationMode(.middle)
                Text(file["authorName"].text + " · " + ByteCountFormatter.string(fromByteCount: Int64(file["size"].int), countStyle: .file)).font(.system(size: 9.5)).foregroundStyle(CompanionTheme.secondary).lineLimit(1)
                if !notice.isEmpty { Text(notice).font(.caption2).foregroundStyle(.red).lineLimit(2) }
            }.frame(width: 108).contentShape(Rectangle())
        }.buttonStyle(.plain).disabled(loading).accessibilityLabel("Preview " + file["name"].text)
        .task(id: chat + file["id"].text) {
            if file["kind"].text == "image", file["size"].int <= 8 * 1024 * 1024 {
                thumbnail = try? await model.fetchThumbnail(file, chat: chat)
            }
        }
        #if os(iOS)
        .sheet(isPresented: $previewing) { if let url { CompanionQuickLook(url: url) } }
        #endif
    }
    private var glyph: String {
        switch file["kind"].text { case "image": return "photo"; case "video": return "film"; case "audio": return "waveform"; case "pdf": return "doc.richtext"; default: return "doc" }
    }
    @MainActor private func load(open: Bool) async {
        guard !loading else { return }
        loading = true; notice = ""
        defer { loading = false }
        do {
            url = try await model.fetchAttachment(file, chat: chat)
            if open {
                #if os(iOS)
                previewing = true
                #else
                if let url { NSWorkspace.shared.open(url) }
                #endif
            }
        } catch { notice = error.localizedDescription }
    }
}

#if os(iOS)
private struct CompanionQuickLook: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url) }
    func makeUIViewController(context: Context) -> QLPreviewController {
        let controller = QLPreviewController(); controller.dataSource = context.coordinator; return controller
    }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) {}
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(_ url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}
#endif
