"""
PatchIterationDriver

Controls patch-level iterations.

Budget semantics:
    -1 → unlimited
"""

from retrieval_state import RetrievalState, RetrievalBudgets
from attempt_controller import AttemptController


class PatchIterationDriver:

    def __init__(
        self,
        max_iterations: int,
        max_queries_per_iteration: int,
        max_replans: int,
        max_depth_increases: int,
    ):
        self.max_iterations = max_iterations
        self.max_queries_per_iteration = max_queries_per_iteration
        self.max_replans = max_replans
        self.max_depth_increases = max_depth_increases

    def run(self):

        iteration = 0

        while self.max_iterations == -1 or iteration < self.max_iterations:

            iteration += 1
            print(f"\n[PATCH ITERATION {iteration}]")

            budgets = RetrievalBudgets(
                max_queries_per_iteration=self.max_queries_per_iteration,
                max_replans=self.max_replans,
                max_depth_increases_per_task=self.max_depth_increases,
            )

            state = RetrievalState(budgets)
            controller = AttemptController(state)

            stop_reason = controller.run()

            # retrieval objective met → generate patch
            print("[GENERATE PATCH]")
            patch_success = False  # simulated

            if patch_success:
                print("[PATCH SUCCESS]")
                return "PATCH_SUCCESS"

        print("[PATCH ITERATION LIMIT REACHED]")
        return "PATCH_ITERATION_LIMIT_REACHED"