import SwiftUI

/// The plan card: a Cursor-style task panel showing the decomposed goal as a
/// live checklist. Header carries the goal title, the ``X/N complete`` count,
/// a segmented progress bar (one segment per step — accent when the step is
/// done or running, muted when pending) and a collapse chevron. Rows show a
/// status bullet plus a human label (``Running · now`` / ``Pending`` /
/// ``Completed``), so a run's progress is readable at a glance and every
/// update the CLI streams repaints the card in place.
struct PlanChecklistView: View {
    @ObservedObject var state: AppState
    let entry: TimelineEntry
    @State private var isExpanded = true

    private var steps: [PlanStep] { entry.planSteps }
    private var completedCount: Int { steps.filter { $0.status == "completed" }.count }
    private var goalTitle: String {
        entry.planGoal ?? state.threadsStore.activeThread?.title ?? entry.title
    }

    private var canRun: Bool {
        state.activeThread?.previewGoal != nil && state.activeThread?.status == .draft
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .contentShape(Rectangle())
                .onTapGesture { withAnimation(.easeInOut(duration: 0.18)) { isExpanded.toggle() } }
                .hoverPointer(radius: 6)

            if isExpanded {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(steps) { step in
                        row(for: step)
                    }
                }
                .padding(.top, 10)

                if canRun {
                    Button {
                        state.executePreview()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "play.fill").font(.system(size: 10, weight: .semibold))
                            Text("Çalıştır").font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundStyle(Theme.canvas)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 7)
                        .background(Theme.accent)
                        .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .hoverPointer(radius: 12)
                    .padding(.top, 10)
                    .help("Run this plan for real")
                }
            }
        }
        .font(.system(size: 12))
        .padding(12)
        .cardStyle(cornerRadius: 10)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "checklist")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.accent)

            Text(goalTitle)
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(1)

            Spacer(minLength: 6)

            Text("\(completedCount)/\(steps.count) complete")
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(Theme.textSecondary)

            progressSegments

            Image(systemName: "chevron.down")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(Theme.textMuted)
                .rotationEffect(.degrees(isExpanded ? 0 : -90))
        }
    }

    /// One thin capsule per plan step: accent when the step is done or
    /// running, muted while pending — the same visual language as the row
    /// bullets, condensed into the header.
    private var progressSegments: some View {
        HStack(spacing: 3) {
            ForEach(steps.prefix(8), id: \.id) { step in
                Capsule()
                    .fill(
                        step.status == "completed" || step.status == "in_progress"
                            ? Theme.accent
                            : Theme.borderOverlay
                    )
                    .frame(width: 14, height: 4)
            }
        }
    }

    // MARK: - Rows

    private func row(for step: PlanStep) -> some View {
        Button {
            state.scrollTargetID = step.targetEntryID ?? entry.id
        } label: {
            HStack(alignment: .center, spacing: 8) {
                bullet(for: step)
                Text(step.description)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textPrimary)
                    .multilineTextAlignment(.leading)
                Spacer(minLength: 6)
                statusLabel(for: step)
            }
            .padding(.vertical, 5)
            .padding(.horizontal, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .hoverPointer(radius: 6)
    }

    private func bullet(for step: PlanStep) -> some View {
        switch step.status {
        case "completed":
            return AnyView(
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 13)).foregroundStyle(Theme.accent)
            )
        case "in_progress":
            return AnyView(
                Image(systemName: "circle.inset.filled")
                    .font(.system(size: 13)).foregroundStyle(Theme.accent)
            )
        case "failed":
            return AnyView(
                Image(systemName: "exclamationmark.circle")
                    .font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
            )
        default:
            return AnyView(
                Image(systemName: "circle")
                    .font(.system(size: 13)).foregroundStyle(Theme.textMuted)
            )
        }
    }

    @ViewBuilder
    private func statusLabel(for step: PlanStep) -> some View {
        switch step.status {
        case "completed":
            Text("Completed")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(Theme.accent)
        case "in_progress":
            HStack(spacing: 3) {
                Text("Running").font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.textPrimary)
                Text("now").font(.system(size: 10))
                    .foregroundStyle(Theme.textMuted)
            }
        case "failed":
            Text("Failed")
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(Theme.textSecondary)
        default:
            Text("Pending")
                .font(.system(size: 10))
                .foregroundStyle(Theme.textMuted)
        }
    }
}