import SwiftUI

/// Control & Security governance view:
/// - Pending approvals ("Pending Approvals") with Approve / Always / Deny
/// - Blocked missions ("Paused Tasks") with resume / cancel
/// - Active security grants ("Active Security Grants") with revoke action
/// - Security audit report generator ("Security Audit Report")
struct ControlView: View {
    @ObservedObject var state: AppState
    @ObservedObject var store = StoreDataService.shared

    @State private var selectedReportHours: Double = 24.0

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                headerSection

                pendingApprovalsSection

                blockedMissionsSection

                activeGrantsSection

                auditReportSection

                Spacer(minLength: 40)
            }
            .padding(.horizontal, 32)
            .padding(.top, 24)
            .padding(.bottom, 40)
            .frame(maxWidth: 880, alignment: .leading)
        }
        .background(Color.clear)
        .onAppear {
            store.loadControlRecords()
        }
    }

    // MARK: - Header

    private var headerSection: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Image(systemName: "shield.checkered")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Theme.accent)
                    Text("Control & Security")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)

                    if !store.pendingApprovals.isEmpty {
                        Text("\(store.pendingApprovals.count) Pending")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Theme.cardElevated)
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Theme.borderOverlay, lineWidth: 1))
                    }
                }

                Text("Manage agent authorizations, security decisions, and system access logs")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer()

            Button {
                store.loadControlRecords()
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 28, height: 28)
                    .background(Theme.card)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .stroke(Theme.borderOverlay, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
            .hoverPointer(bg: Theme.cardElevated, radius: 6)
            .help("Reload security data")
        }
    }

    // MARK: - Section 1: Pending Approvals

    private var pendingApprovalsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary)
                Text("PENDING APPROVALS")
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.8)
            }

            if store.pendingApprovals.isEmpty {
                emptySectionCard(text: "No pending approvals. The agent is operating within safe boundaries.")
            } else {
                VStack(spacing: 10) {
                    ForEach(store.pendingApprovals) { item in
                        approvalCard(item)
                    }
                }
            }
        }
    }

    private func approvalCard(_ item: PendingApprovalItem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.subGoal.isEmpty ? item.actionType : item.subGoal)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)

                    HStack(spacing: 6) {
                        Text(item.actionType)
                            .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                            .foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Theme.cardElevated)
                            .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
                            .overlay(
                                RoundedRectangle(cornerRadius: 4, style: .continuous)
                                    .stroke(Theme.borderOverlay, lineWidth: 1)
                            )

                        if !item.targetLabel.isEmpty {
                            Text(item.targetLabel)
                                .font(.system(size: 11))
                                .foregroundStyle(Theme.textMuted.opacity(0.8))
                        }

                        Text("· Risk: \(item.risk)")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(Theme.textSecondary)
                    }
                }

                Spacer()
            }

            HStack(spacing: 8) {
                Button {
                    store.decideApproval(id: item.requestId, approve: true, always: false)
                } label: {
                    Text("Approve")
                        .font(.system(size: 11.5, weight: .semibold))
                        .foregroundStyle(Theme.canvas)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5.5)
                        .background(Theme.accent)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                }
                .buttonStyle(.plain)
                .hoverPointer(bg: Theme.accent.opacity(0.85), radius: 5)

                Button {
                    store.decideApproval(id: item.requestId, approve: true, always: true)
                } label: {
                    Text("Always Allow")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5.5)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 5, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(bg: Theme.cardElevated, radius: 5)

                Spacer()

                Button {
                    store.decideApproval(id: item.requestId, approve: false, always: false)
                } label: {
                    Text("Reject")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 5.5)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 5, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(radius: 5)
            }
        }
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Theme.borderOverlay.opacity(0.8), lineWidth: 1)
        )
    }

    // MARK: - Section 2: Paused Tasks

    private var blockedMissionsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "pause.circle.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary)
                Text("PAUSED TASKS")
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.8)
            }

            if store.blockedMissions.isEmpty {
                emptySectionCard(text: "No paused tasks.")
            } else {
                VStack(spacing: 10) {
                    ForEach(store.blockedMissions) { mission in
                        blockedMissionCard(mission)
                    }
                }
            }
        }
    }

    private func blockedMissionCard(_ mission: BlockedMissionItem) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(mission.missionId)
                    .font(.system(size: 12.5, weight: .medium, design: .monospaced))
                    .foregroundStyle(Theme.textPrimary)

                Text(mission.blockedReason.isEmpty ? mission.goal : mission.blockedReason)
                    .font(.system(size: 11.5))
                    .foregroundStyle(Theme.textMuted)
            }

            Spacer()

            HStack(spacing: 8) {
                Button {
                    store.resumeMission(id: mission.missionId)
                } label: {
                    Text("Resume")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 5, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(radius: 5)

                Button {
                    store.cancelMission(id: mission.missionId)
                } label: {
                    Text("Cancel")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 5, style: .continuous)
                                .stroke(Theme.borderOverlay, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .hoverPointer(radius: 5)
            }
        }
        .padding(12)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Theme.borderOverlay, lineWidth: 1)
        )
    }

    // MARK: - Section 3: Active Grants

    private var activeGrantsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "key.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.textSecondary)
                Text("ACTIVE SECURITY GRANTS")
                    .font(.system(size: 11.5, weight: .bold))
                    .foregroundStyle(Theme.textMuted)
                    .tracking(0.8)
            }

            if store.activeGrants.isEmpty {
                emptySectionCard(text: "No active security grants.")
            } else {
                VStack(spacing: 8) {
                    ForEach(store.activeGrants) { grant in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 6) {
                                    Text(grant.verb)
                                        .font(.system(size: 11.5, weight: .bold, design: .monospaced))
                                        .foregroundStyle(Theme.textSecondary)
                                    Text(grant.targetPattern)
                                        .font(.system(size: 12, weight: .medium, design: .monospaced))
                                        .foregroundStyle(Theme.textPrimary)
                                }
                                Text("Application: \(grant.app.isEmpty ? "*" : grant.app) · Remaining uses: \(grant.remainingUses)")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(Theme.textMuted)
                            }
                            Spacer()
                            Button {
                                store.revokeGrant(id: grant.grantId)
                            } label: {
                                Image(systemName: "trash")
                                    .font(.system(size: 11))
                                    .foregroundStyle(Theme.textSecondary)
                                    .frame(width: 24, height: 24)
                            }
                            .buttonStyle(.plain)
                            .hoverPointer(radius: 4)
                            .help("Revoke this grant")
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Theme.cardElevated)
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    }
                }
                .padding(10)
                .background(Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(Theme.borderOverlay, lineWidth: 1)
                )
            }
        }
    }

    // MARK: - Section 4: Security Audit Report

    private var auditReportSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center) {
                HStack(spacing: 6) {
                    Image(systemName: "doc.text.magnifyingglass")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textSecondary)
                    Text("SECURITY AUDIT REPORT")
                        .font(.system(size: 11.5, weight: .bold))
                        .foregroundStyle(Theme.textMuted)
                        .tracking(0.8)
                }

                Spacer()

                HStack(spacing: 6) {
                    reportIntervalButton(title: "1h", hours: 1)
                    reportIntervalButton(title: "24h", hours: 24)
                    reportIntervalButton(title: "7d", hours: 168)
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                if store.isLoadingReport {
                    HStack(spacing: 8) {
                        ProgressView()
                            .controlSize(.small)
                            .tint(Theme.accent)
                        Text("Generating report...")
                            .font(.system(size: 11.5))
                            .foregroundStyle(Theme.textMuted)
                    }
                    .padding(14)
                } else {
                    ScrollView(.horizontal, showsIndicators: false) {
                        Text(store.auditReportText.isEmpty ? "Could not load report or no report requested yet." : store.auditReportText)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(Theme.textPrimary.opacity(0.9))
                            .textSelection(.enabled)
                            .padding(14)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.canvas)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Theme.borderOverlay, lineWidth: 1)
            )
        }
    }

    private func reportIntervalButton(title: String, hours: Double) -> some View {
        let isSelected = selectedReportHours == hours

        return Button {
            selectedReportHours = hours
            store.generateReport(hours: hours)
        } label: {
            Text(title)
                .font(.system(size: 11, weight: isSelected ? .semibold : .regular))
                .foregroundStyle(isSelected ? Theme.textPrimary : Theme.textMuted)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(isSelected ? Theme.cardElevated : Theme.card)
                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .stroke(isSelected ? Theme.textSecondary.opacity(0.4) : Color.clear, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .hoverPointer(radius: 5)
    }

    // MARK: - Helpers

    private func emptySectionCard(text: String) -> some View {
        HStack {
            Text(text)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textMuted)
            Spacer()
        }
        .padding(14)
        .background(Theme.card)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(Theme.borderOverlay.opacity(0.7), lineWidth: 1)
        )
    }
}
