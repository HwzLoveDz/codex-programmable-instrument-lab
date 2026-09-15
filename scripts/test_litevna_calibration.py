#!/usr/bin/env python3

import unittest

import litevna_calibration as calibration


class LiteVNACalibrationMathTest(unittest.TestCase):
    def test_ideal_osl_solution_and_correction(self):
        terms = calibration.solve_one_port(1 + 0j, -1 + 0j, 0 + 0j)
        self.assertEqual(terms, (0j, 1 + 0j, 0j))
        self.assertAlmostEqual(calibration.correct_one_port(0.25 + 0.5j, terms).real, 0.25)
        self.assertAlmostEqual(calibration.correct_one_port(0.25 + 0.5j, terms).imag, 0.5)

    def test_nonideal_error_model_is_inverted(self):
        directivity = 0.03 + 0.01j
        tracking = 0.8 - 0.1j
        source_match = 0.1 + 0.02j

        def measure(gamma):
            return directivity + tracking * gamma / (1 - source_match * gamma)

        terms = calibration.solve_one_port(measure(1), measure(-1), measure(0))
        expected = 0.2 - 0.3j
        corrected = calibration.correct_one_port(measure(expected), terms)
        self.assertAlmostEqual(corrected.real, expected.real, places=12)
        self.assertAlmostEqual(corrected.imag, expected.imag, places=12)

    def test_forward_response_removes_isolation_and_thru(self):
        self.assertAlmostEqual(calibration.correct_forward(0.41 + 0j, 0.01 + 0j, 0.81 + 0j).real, 0.5)

    def test_rejects_degenerate_standards(self):
        with self.assertRaises(ValueError):
            calibration.solve_one_port(1 + 0j, 1 + 0j, 0j)
        with self.assertRaises(ValueError):
            calibration.correct_forward(0.2 + 0j, 0.1 + 0j, 0.1 + 0j)


if __name__ == "__main__":
    unittest.main()
