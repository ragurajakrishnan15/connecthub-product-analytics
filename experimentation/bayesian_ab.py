"""
Bayesian A/B Testing
Beta-Binomial conjugate model with probability of improvement.
"""
import numpy as np


def bayesian_ab_test(
    control_successes, control_trials,
    treatment_successes, treatment_trials,
    prior_alpha=1, prior_beta=1,
    n_simulations=100_000
):
    """Beta-Binomial Bayesian A/B test with probability of improvement."""
    np.random.seed(42)

    control_posterior = np.random.beta(
        prior_alpha + control_successes,
        prior_beta + control_trials - control_successes,
        n_simulations
    )
    treatment_posterior = np.random.beta(
        prior_alpha + treatment_successes,
        prior_beta + treatment_trials - treatment_successes,
        n_simulations
    )

    prob_treatment_wins = np.mean(treatment_posterior > control_posterior)
    lift_samples = (treatment_posterior - control_posterior) / control_posterior
    expected_lift = np.mean(lift_samples)
    lift_ci = np.percentile(lift_samples, [2.5, 97.5])

    # Expected loss (risk of choosing treatment if it's actually worse)
    loss_if_treatment = np.mean(
        np.maximum(control_posterior - treatment_posterior, 0)
    )

    return {
        'prob_treatment_better': float(round(prob_treatment_wins, 4)),
        'expected_lift': float(round(expected_lift, 4)),
        'lift_ci_95': [float(round(lift_ci[0], 4)), float(round(lift_ci[1], 4))],
        'risk_of_choosing_treatment': float(round(1 - prob_treatment_wins, 4)),
        'expected_loss': float(round(loss_if_treatment, 6)),
        'recommendation': (
            'Ship treatment' if prob_treatment_wins > 0.95
            else 'Continue experiment' if prob_treatment_wins > 0.80
            else 'Consider reverting'
        )
    }


if __name__ == '__main__':
    # Example: treatment has 340/1000 vs control 310/1000
    result = bayesian_ab_test(310, 1000, 340, 1000)
    print("Bayesian A/B Test Results:")
    for k, v in result.items():
        print(f"  {k}: {v}")
