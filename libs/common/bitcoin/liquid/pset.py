import sys

if sys.implementation.name == "micropython":
    import secp256k1
else:
    from ..util import secp256k1

from .. import compact, hashes
from ..psbt import *
from collections import OrderedDict
from io import BytesIO
from .transaction import LTransaction, LTransactionOutput, LTransactionInput, TxOutWitness, Proof, LSIGHASH, unblind
from . import slip77
import hashlib, gc

class LInputScope(InputScope):
    TX_CLS = LTransaction
    TXOUT_CLS = LTransactionOutput

    def __init__(self, unknown: dict = {}, **kwargs):
        # liquid-specific fields:
        self.value = None
        self.value_blinding_factor = None
        self.asset = None
        self.asset_blinding_factor = None
        self.range_proof = None
        super().__init__(unknown, **kwargs)

    def clear_metadata(self):
        """Removes metadata like derivations, utxos etc except final or partial sigs"""
        super().clear_metadata()
        self.range_proof = None
        self.value_blinding_factor = None
        self.asset_blinding_factor = None
        self.value = None
        self.asset = None

    def unblind(self, blinding_key):
        if self.range_proof is None:
            return

        pk = slip77.blinding_key(blinding_key, self.utxo.script_pubkey)

        value, asset, vbf, in_abf, extra, min_value, max_value = unblind(
            self.utxo.ecdh_pubkey, pk.secret, self.range_proof, self.utxo.value, self.utxo.asset, self.utxo.script_pubkey
        )
        # verify
        gen = secp256k1.generator_generate_blinded(asset, in_abf)
        assert gen == secp256k1.generator_parse(self.utxo.asset)
        cmt = secp256k1.pedersen_commit(vbf, value, gen)
        assert cmt == secp256k1.pedersen_commitment_parse(self.utxo.value)

        self.asset = asset
        self.value = value
        self.asset_blinding_factor = in_abf
        self.value_blinding_factor = vbf

    @property
    def vin(self):
        return LTransactionInput(self.txid, self.vout, sequence=(self.sequence or 0xFFFFFFFF))

    def read_value(self, stream, k):
        # standard bitcoin stuff
        if (b'\xfc\x08elements' not in k) and (b"\xfc\x04pset" not in k):
            super().read_value(stream, k)
        elif k == b'\xfc\x04pset\x0e':
            # range proof is very large,
            # so we don't load it if compress flag is set.
            if self.compress:
                skip_string(stream)
            else:
                self.range_proof = read_string(stream)
        else:
            v = read_string(stream)
            # liquid-specific fields
            if k == b'\xfc\x08elements\x00':
                self.value = int.from_bytes(v, 'little')
            elif k == b'\xfc\x08elements\x01':
                self.value_blinding_factor = v
            elif k == b'\xfc\x08elements\x02':
                self.asset = v
            elif k == b'\xfc\x08elements\x03':
                self.asset_blinding_factor = v
            else:
                self.unknown[k] = v

    def write_to(self, stream, skip_separator=False, **kwargs) -> int:
        r = super().write_to(stream, skip_separator=True, **kwargs)
        # liquid-specific keys
        if self.value is not None:
            r += ser_string(stream, b'\xfc\x08elements\x00')
            r += ser_string(stream, self.value.to_bytes(8, 'little'))
        if self.value_blinding_factor is not None:
            r += ser_string(stream, b'\xfc\x08elements\x01')
            r += ser_string(stream, self.value_blinding_factor)
        if self.asset is not None:
            r += ser_string(stream, b'\xfc\x08elements\x02')
            r += ser_string(stream, self.asset)
        if self.asset_blinding_factor is not None:
            r += ser_string(stream, b'\xfc\x08elements\x03')
            r += ser_string(stream, self.asset_blinding_factor)
        if self.range_proof is not None:
            r += ser_string(stream, b'\xfc\x04pset\x0e')
            r += ser_string(stream, self.range_proof)
        # separator
        if not skip_separator:
            r += stream.write(b"\x00")
        return r


class LOutputScope(OutputScope):
    def __init__(self, unknown: dict = {}, vout=None, **kwargs):
        # liquid stuff
        self.value_commitment = None
        self.value_blinding_factor = None
        self.asset_commitment = None
        self.asset_blinding_factor = None
        self.range_proof = None
        self.surjection_proof = None
        self.ecdh_pubkey = None
        self.blinding_pubkey = None
        self.asset = None
        self.blinder_index = None
        if vout:
            self.asset = vout.asset
        self._verified = False
        # super calls parse_unknown() at the end
        super().__init__(unknown, vout=vout, **kwargs)

    @property
    def is_verified(self):
        return self._verified

    def verify(self):
        self._verified = False
        gen = None
        e = PSBTError("Invalid commitments")
        if self.asset and self.asset_commitment:
            # we can't verify asset
            if not self.asset_blinding_factor:
                raise e
            gen = secp256k1.generator_generate_blinded(self.asset, self.asset_blinding_factor)
            if self.asset_commitment != secp256k1.generator_serialize(gen):
                raise e

        if self.value and self.value_commitment:
            if gen is None:
                raise e
            value_commitment = secp256k1.pedersen_commit(self.value_blinding_factor, self.value, gen)
            if self.value_commitment != secp256k1.pedersen_commitment_serialize(value_commitment):
                raise e
        self._verified = True
        return self._verified

    def clear_metadata(self):
        """Removes metadata like derivations, utxos etc except final or partial sigs"""
        super().clear_metadata()
        self.range_proof = None
        self.surjection_proof = None
        self.value_blinding_factor = None
        self.asset_blinding_factor = None
        if self.value_commitment:
            self.value = None
        if self.asset_commitment:
            self.asset = None
        self.blinder_index = None

    @property
    def vout(self):
        return LTransactionOutput(
                    self.asset or self.asset_commitment,
                    self.value or self.value_commitment,
                    self.script_pubkey,
                    None if self.asset else self.ecdh_pubkey)

    @property
    def blinded_vout(self):
        return LTransactionOutput(
                    self.asset_commitment or self.asset,
                    self.value_commitment or self.value,
                    self.script_pubkey,
                    self.ecdh_pubkey,
                    None if not self.surjection_proof else TxOutWitness(Proof(self.surjection_proof), Proof(self.range_proof))
        )

    def reblind(self, nonce, blinding_pubkey=None, extra_message=b""):
        """
        Re-generates range-proof with particular nonce
        and includes extra message in the range proof.
        This message can contain some useful info like a label or whatever else.
        """
        if not self.is_blinded:
            return
        # check blinding pubkey is there
        blinding_pubkey = blinding_pubkey or self.blinding_pubkey
        if not blinding_pubkey:
            raise PSBTError("Blinding pubkey required")
        pub = secp256k1.ec_pubkey_parse(blinding_pubkey)
        self.ecdh_pubkey = ec.PrivateKey(nonce).sec()
        secp256k1.ec_pubkey_tweak_mul(pub, nonce)
        sec = secp256k1.ec_pubkey_serialize(pub)
        ecdh_nonce = hashlib.sha256(hashlib.sha256(sec).digest()).digest()
        msg = self.asset[-32:] + self.asset_blinding_factor + extra_message
        self.range_proof = secp256k1.rangeproof_sign(
            ecdh_nonce, self.value, secp256k1.pedersen_commitment_parse(self.value_commitment),
            self.value_blinding_factor, msg,
            self.script_pubkey.data, secp256k1.generator_parse(self.asset_commitment))


    def read_value(self, stream, k):
        if (b'\xfc\x08elements' not in k) and (b"\xfc\x04pset" not in k):
            super().read_value(stream, k)
        # range proof and surjection proof are very large,
        # so we don't load them if compress flag is set.
        elif k in [b'\xfc\x08elements\x04', b'\xfc\x04pset\x04']:
            if self.compress:
                skip_string(stream)
            else:
                self.range_proof = read_string(stream)
        elif k in [b'\xfc\x08elements\x05', b'\xfc\x04pset\x05']:
            if self.compress:
                skip_string(stream)
            else:
                self.surjection_proof = read_string(stream)
        else:
            v = read_string(stream)
            # liquid-specific fields
            if k in [b'\xfc\x08elements\x00', b'\xfc\x04pset\x01']:
                self.value_commitment = v
            elif k == b'\xfc\x08elements\x01':
                self.value_blinding_factor = v
            elif k == b'\xfc\x04pset\x02':
                self.asset = v
            elif k in [b'\xfc\x08elements\x02', b'\xfc\x04pset\x03']:
                self.asset_commitment = v
            elif k == b'\xfc\x08elements\x03':
                self.asset_blinding_factor = v
            elif k in [b'\xfc\x08elements\x06', b'\xfc\x04pset\x06']:
                self.blinding_pubkey = v
            elif k in [b'\xfc\x08elements\x07', b'\xfc\x04pset\x07']:
                self.ecdh_pubkey = v
            elif k == b"\xfc\x04pset\x08":
                self.blinder_index = int.from_bytes(v, 'little')
            else:
                self.unknown[k] = v

    @property
    def is_blinded(self):
        # TODO: not great
        return self.value_commitment and self.asset_commitment

    def write_to(self, stream, skip_separator=False, version=None, **kwargs) -> int:
        # TODO: super.write_to()
        r = super().write_to(stream, skip_separator=True, version=version, **kwargs)
        # liquid-specific keys
        if self.asset is not None and version == 2:
            r += ser_string(stream, b'\xfc\x04pset\x02')
            r += ser_string(stream, self.asset)
        if self.value_commitment is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x01')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x00')
            r += ser_string(stream, self.value_commitment)
        if self.value_blinding_factor is not None:
            r += ser_string(stream, b'\xfc\x08elements\x01')
            r += ser_string(stream, self.value_blinding_factor)
        if self.asset_commitment is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x03')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x02')
            r += ser_string(stream, self.asset_commitment)
        if self.asset_blinding_factor is not None:
            r += ser_string(stream, b'\xfc\x08elements\x03')
            r += ser_string(stream, self.asset_blinding_factor)
        if self.blinding_pubkey is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x06')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x06')
            r += ser_string(stream, self.blinding_pubkey)
        if self.ecdh_pubkey is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x07')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x07')
            r += ser_string(stream, self.ecdh_pubkey)
        # for some reason keys 04 and 05 are serialized after 07
        if self.range_proof is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x04')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x04')
            r += ser_string(stream, self.range_proof)
        if self.surjection_proof is not None:
            if version == 2:
                r += ser_string(stream, b'\xfc\x04pset\x05')
            else:
                r += ser_string(stream, b'\xfc\x08elements\x05')
            r += ser_string(stream, self.surjection_proof)
        if self.blinder_index is not None:
            r += ser_string(stream, b"\xfc\x04pset\x08")
            r += ser_string(stream, self.blinder_index.to_bytes(4, 'little'))
        # separator
        if not skip_separator:
            r += stream.write(b"\x00")
        return r

class PSET(PSBT):
    MAGIC = b"pset\xff"
    PSBTIN_CLS = LInputScope
    PSBTOUT_CLS = LOutputScope
    TX_CLS = LTransaction

    def unblind(self, blinding_key):
        for inp in self.inputs:
            inp.unblind(blinding_key)

    def txseed(self, seed:bytes):
        assert len(seed) == 32
        # get unique seed for this tx:
        # we use seed + txid:vout + scriptpubkey as unique data for tagged hash
        data = b"".join([
            bytes(reversed(inp.txid))+inp.vout.to_bytes(4,'little')
            for inp in self.inputs
        ]) + b"".join([out.script_pubkey.serialize() for out in self.outputs])
        return hashes.tagged_hash("liquid/txseed", seed+data)

    def blind(self, seed:bytes):
        txseed = self.txseed(seed)
        # assign blinding factors to all outputs
        blinding_outs = []
        for i, out in enumerate(self.outputs):
            # skip ones where we don't need blinding
            if out.blinding_pubkey is None or out.value is None:
                continue
            out.asset_blinding_factor = hashes.tagged_hash("liquid/abf", txseed+i.to_bytes(4,'little'))
            out.value_blinding_factor = hashes.tagged_hash("liquid/vbf", txseed+i.to_bytes(4,'little'))
            blinding_outs.append(out)
        if len(blinding_outs) == 0:
            raise PSBTError("Nothing to blind")
        # calculate last vbf
        vals = [sc.value or sc.utxo.value for sc in self.inputs] + [sc.value for sc in blinding_outs]
        abfs = [sc.asset_blinding_factor or b"\x00"*32 for sc in self.inputs + blinding_outs]
        vbfs = [sc.value_blinding_factor or b"\x00"*32 for sc in self.inputs + blinding_outs]
        last_vbf = secp256k1.pedersen_blind_generator_blind_sum(vals, abfs, vbfs, len(self.inputs))
        blinding_outs[-1].value_blinding_factor = last_vbf

        # calculate commitments (surj proof etc)

        in_tags = []
        in_gens = []
        for inp in self.inputs:
            if inp.asset:
                in_tags.append(inp.asset)
                in_gens.append(secp256k1.generator_parse(inp.utxo.asset))
            # if we have unconfidential input
            elif len(inp.utxo.asset) == 32:
                in_tags.append(inp.utxo.asset)
                in_gens.append(secp256k1.generator_generate(inp.utxo.asset))

        for i, out in enumerate(self.outputs):
            if None in [out.blinding_pubkey, out.value, out.asset_blinding_factor]:
                continue
            gen = secp256k1.generator_generate_blinded(out.asset, out.asset_blinding_factor)
            out.asset_commitment = secp256k1.generator_serialize(gen)
            value_commitment = secp256k1.pedersen_commit(out.value_blinding_factor, out.value, gen)
            out.value_commitment = secp256k1.pedersen_commitment_serialize(value_commitment)

            proof_seed = hashes.tagged_hash("liquid/surjection_proof", txseed+i.to_bytes(4,'little'))
            proof, in_idx = secp256k1.surjectionproof_initialize(in_tags, out.asset, proof_seed)
            secp256k1.surjectionproof_generate(proof, in_idx, in_gens, gen, abfs[in_idx], out.asset_blinding_factor)
            out.surjection_proof = secp256k1.surjectionproof_serialize(proof)
            del proof
            gc.collect()

            # generate range proof
            rangeproof_nonce = hashes.tagged_hash("liquid/range_proof", txseed+i.to_bytes(4,'little'))
            out.reblind(rangeproof_nonce)

    def fee(self):
        fee = 0
        for out in self.tx.vout:
            if out.script_pubkey.data == b"":
                fee += out.value
        return fee

    @property
    def blinded_tx(self):
        return self.TX_CLS(version=self.tx_version or 2,
                           locktime=self.locktime or 0,
                           vin=[inp.vin for inp in self.inputs],
                           vout=[out.blinded_vout for out in self.outputs])

    def sighash_segwit(self, input_index, script_pubkey, value, sighash=(LSIGHASH.ALL | LSIGHASH.RANGEPROOF)):
        return self.blinded_tx.sighash_segwit(input_index, script_pubkey, value, sighash)

    def sighash_legacy(self, *args, **kwargs):
        return self.blinded_tx.sighash_legacy(*args, **kwargs)

    # def sign_with(self, root, sighash=(LSIGHASH.ALL | LSIGHASH.RANGEPROOF)) -> int:
    # TODO: change back to sighash rangeproof when deployed
    def sign_with(self, root, sighash=LSIGHASH.ALL) -> int:
        return super().sign_with(root, sighash)

    @property
    def is_verified(self):
        return all([sc.is_verified for sc in self.inputs + self.outputs])

    def verify(self, *args, **kwargs):
        """Checks that all commitments, values and assets are consistent"""
        super().verify(*args, **kwargs)
        for out in self.outputs:
            out.verify()
        return self.is_verified