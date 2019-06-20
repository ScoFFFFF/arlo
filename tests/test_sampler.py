import pytest
import numpy as np

from sampler import Sampler

@pytest.fixture
def sampler():
    seed = 12345678901234567890
    risk_limit = .1
    contests = {
        'test1': {
            'cand1': 600,
            'cand2': 400,
            'ballots': 1000
        },
        'test2': {
            'cand1': 600,
            'cand2': 200,
            'cand3': 100,
            'ballots': 900
        },
        'test3': {
            'cand1': 100,
            'ballots': 100
        },
        'test4': {
            'cand1': 100,
            'ballots': 100
        },
    }



    yield Sampler(seed, risk_limit, contests)

def test_compute_margins(sampler):
    # Test margins
    true_margins = {
        'test1': { 
            'p_w': .6,
            'p_r': .4,
            's_w': .6
        },
        'test2' : {
            'p_w' : .75,
            'p_r' : .25,
            's_w': 2/3,
        },
        'test3': {
            'p_w' : 1,
            'p_r' : 0,
            's_w' : 1
        },
        'test4': {
            'p_w' : 1,
            'p_r' : 0,
            's_w' : 1
        }
    }

    margins = sampler.compute_margins()
    for contest in margins:
        true_margins_for_contest = true_margins[contest]
        computed_margins_for_contest = margins[contest]

        expected =  true_margins_for_contest['p_w']
        computed = computed_margins_for_contest['p_w']
        assert expected == computed, 'p_w failed: got {}, expected {}'.format(computed, expected)
        expected =  true_margins_for_contest['p_r']
        computed = computed_margins_for_contest['p_r']
        assert expected == computed, 'p_r failed: got {}, expected {}'.format(computed, expected)
        expected =  true_margins_for_contest['s_w']
        computed = computed_margins_for_contest['s_w']
        assert expected == computed, 's_w failed: got {}, expected {}'.format(computed, expected)

def test_asn(sampler):
    # Test ASN computation

    true_asns = {
        'test1': 119,
        'test2': 22,
        'test3': 0,
        'test4': 0,
    }

    computed_asns = sampler.get_asns()
    for contest in true_asns:
        expected = true_asns[contest]
        computed = computed_asns[contest]

        assert expected == computed, 'asn failed: got {}, expected {}'.format(computed, expected)

def test_simulate_bravo_round0(sampler):
    # Test bravo sample simulator
    # Test without sample
    expected_mean1 = 118
    r0_sample_win = round0_sample_results['test1']['cand1']
    r0_sample_rup = round0_sample_results['test1']['cand2']

    computed_mean1 = np.mean(sampler.simulate_bravo(10000, 
                                                       .6, 
                                    sample_w=r0_sample_win, 
                                    sample_r=r0_sample_rup))
    delta = expected_mean1 - computed_mean1

    # TODO are these tolerances acceptable?
    assert abs(delta) < 5, 'bravo_simulator failed: got {}, expected {}'.format(computed_mean1, expected_mean1)


def test_simulate_bravo_round1_confirmed(sampler):
    # Test with round-one sample that already confirmed
    expected_mean1 = 0 # Our sample already confirmed our results
    r0_sample_win = round1_sample_results['test1']['cand1']
    r0_sample_rup = round1_sample_results['test1']['cand2']

    computed_mean1 = np.mean(sampler.simulate_bravo(10000, 
                                                       .6, 
                                    sample_w=r0_sample_win, 
                                    sample_r=r0_sample_rup))
    delta = expected_mean1 - computed_mean1

    # TODO are these tolerances acceptable?
    assert abs(delta) < 5, 'bravo_simulator failed: got {}, expected {}'.format(computed_mean1, expected_mean1)

def test_simulate_bravo_round1_unconfirmed(sampler):
    # Test with round-one sample that didn't confirm
    expected_mean1 = 91 
    r0_sample_win = round1_sample_results['test2']['cand1']
    r0_sample_rup = round1_sample_results['test2']['cand2']

    computed_mean1 = np.mean(sampler.simulate_bravo(10000, 
                                                       .6, 
                                    sample_w=r0_sample_win, 
                                    sample_r=r0_sample_rup))
    delta = expected_mean1 - computed_mean1

    # TODO are these tolerances acceptable?
    assert abs(delta) < 5, 'bravo_simulator failed: got {}, expected {}'.format(computed_mean1, expected_mean1)

def test_get_sample_sizes(sampler):
    # Test retrieving menu of sample sizes
    computed_samples = sampler.get_sample_sizes(round0_sample_results)
    for contest in computed_samples:
        for key in true_sample_sizes[contest]:
            if key != 'asn':
                continue
            expected =  true_sample_sizes[contest][key]
            computed = computed_samples[contest][key]
            diff = expected - computed
            # TODO are these tolerances acceptable?
            assert abs(diff) < 10 , '{} sample size for {} failed: got {}, expected {}'.format(key, contest, computed, expected)



def test_draw_sample(sampler):
    # Test getting a sample
    manifest = {
        'pct 1': 25,
        'pct 2': 25,
        'pct 3': 25,
        'pct 4': 25,
    }

    sample = sampler.draw_sample(manifest, 20)

    for i, item in enumerate(sample):
        expected = expected_sample[i]
        assert item == expected, 'Draw sample failed: got {}, expected {}'.format(item, expected)


def test_compute_risk(sampler):
    # Test computing sample
    expected_Ts = {
        'test1': .07,
        'test2': 1.11,
        'test3': 1,
        'test4': 0,
    }

    expected_decisions = {
        'test1': True,
        'test2': False,
        'test3': False,
        'test4': True,
    }

    for contest, sample in round1_sample_results.items():
        T, decision = sampler.compute_risk(contest, sample)
        expected_T = expected_Ts[contest]
        diff = T - expected_T 
        assert abs(diff) < .01, 'Risk compute for {} failed! Expected {}, got {}'.format(contest, expected_Ts[contest], T)
        
        expected_decision = expected_decisions[contest]
        assert decision == expected_decision, 'Risk decision for {} failed! Expected {}, got{}'.format(contest, expected_decision, decision)
        
        

# Useful test data
round0_sample_results = {
    'test1': {
        'cand1': 0,
        'cand2': 0,
    },
    'test2': {
        'cand1': 0,
        'cand2': 0,
        'cand3': 0,
    },
    'test3': {
        'cand1': 0,
    },
    'test4': {
        'cand1': 0,
    },
}


round1_sample_results = {
    'test1': {
        'cand1': 72,
        'cand2': 47
    },
    'test2': {
        'cand1': 25,
        'cand2': 18,
        'cand3': '5',
    },
    'test3': {
        'cand1': 0
    },
    'test4': {
        'cand1': 100
    }
}


expected_sample = [
    ('pct 1', 4),
    ('pct 1', 12),
    ('pct 1', 19),
    ('pct 1', 21),
    ('pct 1', 22),
    ('pct 1', 24),
    ('pct 2', 2),
    ('pct 2', 5),
    ('pct 2', 6),
    ('pct 2', 6),
    ('pct 2', 15),
    ('pct 2', 21),
    ('pct 2', 23),
    ('pct 4', 7),
    ('pct 4', 11),
    ('pct 4', 14),
    ('pct 4', 18),
    ('pct 4', 19),
    ('pct 4', 21),
    ('pct 4', 23),
]


true_sample_sizes = {
    'test1': {
        'asn': 119,
        '70%': 130,
        '80%': 170,
        '90%': 243,
    }, 
    'test2': {
        'asn': 22,
        '70%': 19,
        '80%': 24,
        '90%': 38,
    },
    'test3': {
        'asn': 0,
        '70%': 0,
        '80%': 0,
        '90%': 0,
    },
    'test4': {
        'asn': 0,
        '70%': 0,
        '80%': 0,
        '90%': 0,
    },
}