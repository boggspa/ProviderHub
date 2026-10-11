import SwiftUI

@MainActor extension ChatModel {
    var canControlTeamMember: Bool {
        connected && stateChatID != nil && !interrupting && !branchBusy && teamRequest == nil && team?.enabled == true
    }

    func controlTeamMember(_ member: String, start: Bool) {
        guard canControlTeamMember, let owner = stateChatID else { return }
        let request = UUID().uuidString
        teamRequest = request; teamNotice = ""
        if !inspectorCommand(["command": start ? "team_member_start" : "team_member_stop",
                              "id": owner, "member": member, "request": request]) {
            teamRequest = nil; teamNotice = "Chat is disconnected."
        }
    }
}
