"""
This script generates VMTests that are used to check EVM's Frontier fork correctness.

### TO GENERATE ALL:

## Initialize env:
cd manticore/tests/ethereum && mkdir VMTests_concrete && mkdir VMTests_symbolic
git clone https://github.com/ethereum/tests --depth=1

## Generate concrete tests:
for i in ./tests/VMTests/*; do python ../auto_generators/make_VMTests.py $i; done

## Generate symbolic tests:
$ for i in ./tests/VMTests/*; do python ../auto_generators/make_VMTests.py $i --symbolic; done

## Remove the eth tests repo
$ rm -rf ./tests  # cleanup/remove the ethereum/tests repo


### To test just one:
python ../auto_generators/make_VMTests.py ./tests/VMTests/VMTests
"""
import json
import os
import sys
from binascii import unhexlify

import hashlib
import pyevmasm as EVMAsm


def gen_test(testcase, filename, symbolic, skip):
    output = '''    @unittest.skip('Gas or performance related')\n''' if skip else ''

    # Sanity checks

    # We don't use those!
    testcase.pop('_info')
    assert len(testcase.pop('callcreates', [])) == 0

    output += generate_pre_output(testcase, filename, symbolic)

    if 'post' not in testcase:
        output += '''
        # If test end in exception check it here
        self.assertTrue(result == 'THROW')'''
    else:
        output += generate_post_output(testcase)

    return output


def generate_pre_output(testcase, filename, symbolic):
    testname = (os.path.split(filename)[1].replace('-', '_')).split('.')[0]
    bytecode = unhexlify(testcase['exec']['code'][2:])
    disassemble = ''
    try:
        # add silent=True when evmasm supports it
        disassemble = '\n                  '.join(EVMAsm.disassemble(bytecode).split('\n'))
    except Exception as e:
        pass

    with open(filename, 'rb') as f:
        sha256sum = hashlib.sha256(f.read()).hexdigest()

    output = f'''
    def test_{testname}(self):
        """
        Textcase taken from https://github.com/ethereum/tests
        File: {os.path.split(filename)[1]}
        sha256sum: {sha256sum}
        Code:     {disassemble}
        """    
    '''

    if symbolic:
        output += '''
        solver = Z3Solver()

        def solve(val):
            results = solver.get_all_values(constraints, val)
            # We constrain all values to single values!
            self.assertEqual(len(results), 1)
            return results[0]
'''
    else:
        output += '''
        def solve(val):
            """
            Those tests are **auto-generated** and `solve` is used in symbolic tests.
            So yes, this returns just val; it makes it easier to generate tests like this.
            """
            return val
'''

    env = testcase['env']

    gaslimit = int(env['currentGasLimit'], 0)
    blocknumber = int(env['currentNumber'], 0)
    timestamp = int(env['currentTimestamp'], 0)
    difficulty = int(env['currentDifficulty'], 0)
    coinbase = int(env['currentCoinbase'], 0)
    output += f'''
        constraints = ConstraintSet()
'''

    def format_bitvec(name, val, bits=256, symbolic_name=None):
        if symbolic_name is None:
            symbolic_name = name

        return f'''
        {name} = constraints.new_bitvec({bits}, name='{symbolic_name}')
        constraints.add({name} == {val})
'''

    def format_var(name, val):
        return f'''
        {name} = {val}'''

    # Spawns/creates bitvectors in symbolic or pure variables in concrete
    formatter = format_bitvec if symbolic else format_var
    for var in ('blocknumber', 'timestamp', 'difficulty', 'coinbase', 'gaslimit'):
        output += formatter(var, locals()[var])

    output += f'''
        world = evm.EVMWorld(constraints, blocknumber=blocknumber, timestamp=timestamp, difficulty=difficulty,
                             coinbase=coinbase, gaslimit=gaslimit)
    '''

    for address, account in testcase['pre'].items():
        assert account.keys() == {'code', 'nonce', 'balance', 'storage'}

        account_address = int(address, 0)
        account_code = account['code'][2:]
        account_nonce = int(account['nonce'], 0)
        account_balance = int(account['balance'], 0)

        if symbolic:
            postfix = hex(account_address)
            output += f'''
        acc_addr = {hex(account_address)}
        acc_code = unhexlify('{account_code}')
            '''
            output += format_bitvec('acc_balance', account_balance, symbolic_name=f'balance_{postfix}')
            output += format_bitvec('acc_nonce', account_nonce, symbolic_name=f'nonce_{postfix}')
        else:
            output += f'''
        acc_addr = {hex(account_address)}
        acc_code = unhexlify('{account_code}')
        acc_balance = {account_balance}
        acc_nonce = {account_nonce}
'''

        output += f'''
        world.create_account(address=acc_addr, balance=acc_balance, code=acc_code, nonce=acc_nonce)
'''
        for key, value in account['storage'].items():
            if symbolic:
                postfix = hex(account_address)
                output += format_bitvec('key', key, symbolic_name=f'storage_key_{postfix}')
                output += format_bitvec('value', value, symbolic_name=f'storage_value_{postfix}')
                output += '''
        world.set_storage_data(acc_addr, key, value)
'''
            else:
                output += f'''
        world.set_storage_data(acc['address'], {key}, {value})
'''

    address = int(testcase['exec']['address'], 0)
    caller = int(testcase['exec']['caller'], 0)
    code = testcase['exec']['code'][2:]
    calldata = testcase['exec']['data'][2:]
    gas = int(testcase['exec']['gas'], 0)
    price = int(testcase['exec']['gasPrice'], 0)
    origin = int(testcase['exec']['origin'], 0)
    value = int(testcase['exec']['value'], 0)
    assert testcase['exec'].keys() == {'address', 'caller', 'code', 'data', 'gas', 'gasPrice', 'origin', 'value'}

    # Need to check if origin is diff from caller. we do not support those tests
    assert origin == caller, "test type not supported"
    assert testcase['pre']['0x{:040x}'.format(address)]['code'][2:] == code, "test type not supported"

    output += f'''
        address = {hex(address)}
        caller = {hex(caller)}'''

    if symbolic:
        output += format_bitvec('price', price)
        output += format_bitvec('value', value)
        output += format_bitvec('gas', gas)
    else:
        output += f'''
        price = {hex(price)}
        value = {value}
        gas = {gas}'''

    if calldata:
        if symbolic:
            output += f'''
        data = constraints.new_array(index_max={len(calldata)})
        constraints.add(data == '{calldata}')
'''
        else:
            output += f'''
        data = unhexlify('{calldata}')'''
    else:
        output += f"""
        data = ''"""

    output += f'''
        # open a fake tx, no funds send
        world._open_transaction('CALL', address, price, data, caller, value, gas=gas)

        # This variable might seem redundant in some tests - don't forget it is auto generated
        # and there are cases in which we need it ;)
        result = None
        returndata = b''
        try:
            while True:
                world.current_vm.execute()
        except evm.EndTx as e:
            result = e.result
            if result in ('RETURN', 'REVERT'):
                returndata = solve(e.data)
        except evm.StartTx as e:
            self.fail('This tests should not initiate an internal tx (no CALLs allowed)')

        # World sanity checks - those should not change, right?
        self.assertEqual(solve(world.block_number()), {blocknumber})
        self.assertEqual(solve(world.block_gaslimit()), {gaslimit})
        self.assertEqual(solve(world.block_timestamp()), {timestamp})
        self.assertEqual(solve(world.block_difficulty()), {difficulty})
        self.assertEqual(solve(world.block_coinbase()), {coinbase})
'''

    return output


def generate_post_output(testcase):
    output = ''

    for address, account in testcase['post'].items():
        assert account.keys() == {'code', 'nonce', 'balance', 'storage'}

        account_address = int(address, 0)
        account_code = account['code'][2:]
        account_nonce = int(account['nonce'], 0)
        account_balance = int(account['balance'], 0)

        output += f'''
        # Add post checks for account {hex(account_address)}
        # check nonce, balance, code
        self.assertEqual(solve(world.get_nonce({hex(account_address)})), {account_nonce})
        self.assertEqual(solve(world.get_balance({hex(account_address)})), {account_balance})
        self.assertEqual(world.get_code({hex(account_address)}), unhexlify('{account_code}'))'''

    if account['storage']:
        output += '''
        # check storage'''

        for key, value in account['storage'].items():
            output += f'''
        self.assertEqual(solve(world.get_storage_data({hex(account_address)}, {key})), {value})'''

    output += f'''
        # check outs
        self.assertEqual(returndata, unhexlify('{testcase['out'][2:]}'))'''

    output += '''
        # check logs
        logs = [Log(unhexlify('{:040x}'.format(l.address)), l.topics, to_constant(l.memlog)) for l in world.logs]
        data = rlp.encode(logs)'''
    output += f'''
        self.assertEqual(sha3.keccak_256(data).hexdigest(), '{testcase['logs'][2:]}')
'''

    output += f'''
        # test used gas
        self.assertEqual(solve(world.current_vm.gas), {int(testcase['gas'], 0)})'''

    return output


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit()

    symbolic = '--symbolic' in sys.argv

    filename_or_folder = os.path.abspath(sys.argv[1])

    output = f'''"""DO NOT MODIFY: Tests generated from `{os.path.join(*filename_or_folder.split('/')[-2:])}` with make_VMTests.py"""
import unittest
from binascii import unhexlify

import rlp
import sha3
from rlp.sedes import (
    CountableList,
    BigEndianInt,
    Binary,
)

from manticore.core.smtlib import ConstraintSet, Z3Solver  # Ignore unused import in non-symbolic tests!
from manticore.core.smtlib.visitors import to_constant
from manticore.platforms import evm


class Log(rlp.Serializable):
    fields = [
        ('address', Binary.fixed_length(20, allow_empty=True)),
        ('topics', CountableList(BigEndianInt(32))),
        ('data', Binary())
    ]


class EVMTest_{os.path.splitext(os.path.basename(filename_or_folder))[0]}(unittest.TestCase):
    # https://nose.readthedocs.io/en/latest/doc_tests/test_multiprocess/multiprocess.html#controlling-distribution
    _multiprocess_can_split_ = True
    # https://docs.python.org/3.7/library/unittest.html#unittest.TestCase.maxDiff
    maxDiff = None

    SAVED_DEFAULT_FORK = evm.DEFAULT_FORK

    @classmethod
    def setUpClass(cls):
        evm.DEFAULT_FORK = 'frontier'

    @classmethod
    def tearDownClass(cls):
        evm.DEFAULT_FORK = cls.SAVED_DEFAULT_FORK
'''

    if os.path.isdir(filename_or_folder):
        folder = filename_or_folder

        print(f'Generating tests from dir {folder}...', file=sys.stderr)
        cnt = 0

        for filename in os.listdir(folder):
            if not filename.endswith('.json'):
                continue

            filename = os.path.join(folder, filename)
            testcase = dict(json.loads(open(filename).read()))
            for name, testcase in testcase.items():
                output += gen_test(testcase, filename, symbolic=symbolic, skip='Performance' in filename) + '\n'
                cnt += 1

        print(f'Generated {cnt} testcases from jsons in {folder}.', file=sys.stderr)
    else:
        folder = None
        filename = os.path.abspath(filename_or_folder)
        testcase = dict(json.loads(open(filename).read()))
        for name, testcase in testcase.items():
            output += gen_test(testcase, filename, symbolic=symbolic, skip='Performance' in filename) + '\n'

    output += '''

if __name__ == '__main__':
    unittest.main()
'''
    postfix = 'symbolic' if symbolic else 'concrete'

    if folder:
        testname = folder.split('/')[-1]
        with open(f'VMTests_{postfix}/test_{testname}.py', 'w') as f:
            f.write(output)
        with open(f'VMTests_{postfix}/__init__.py', 'w') as f:
            f.write("# DO NOT DELETE")
        print("Tests generated. If this is the only output, u did sth bad.", file=sys.stderr)
    else:
        print(output)
