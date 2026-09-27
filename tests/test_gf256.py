"""Erasure-code algebra checks before binding shards to HDF5 evidence."""

import os
import unittest

from h5reclaim.gf256 import encode_parity, recover_data


class Gf256Tests(unittest.TestCase):
    def test_any_two_data_losses_from_two_parity_shards(self):
        payloads = [os.urandom(127) for _ in range(7)]
        parity = encode_parity(payloads, 2)
        for first in range(len(payloads)):
            for second in range(first + 1, len(payloads)):
                known = {index: value for index, value in enumerate(payloads)
                         if index not in (first, second)}
                self.assertEqual(recover_data(len(payloads), known, parity),
                                 {first: payloads[first], second: payloads[second]})

    def test_four_loss_limit_and_huge_shard_refusal(self):
        payloads = [bytes([index]) * 33 for index in range(8)]
        parity = encode_parity(payloads, 4)
        self.assertEqual(recover_data(8, {index: payloads[index] for index in (0, 3, 5, 7)}, parity),
                         {index: payloads[index] for index in (1, 2, 4, 6)})
        with self.assertRaisesRegex(ValueError, "more missing"):
            recover_data(8, {0: payloads[0], 3: payloads[3], 7: payloads[7]}, parity)
        with self.assertRaisesRegex(ValueError, "bounded"):
            encode_parity([b"x" * (1_048_576 + 1)], 1)


if __name__ == "__main__":
    unittest.main()
