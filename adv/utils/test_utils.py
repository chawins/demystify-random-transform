import pickle

import numpy as np
import torch

from ..attacks import AutoAttack, PGDAttack, setup_attack
from .utils import (compute_int, compute_loss, parse_model_name,
                    select_criterion, set_random_seed)

INFTY = float('inf')


def _compute_grad_stats(stats, grad_list, batch_size):
    TRIM_PERCENT = 0.
    grad = torch.cat(grad_list, dim=1)
    num_eval = grad.size(1)
    for i in range(batch_size):
        begin = int(num_eval * TRIM_PERCENT)
        end = int(num_eval * (1 - TRIM_PERCENT))
        idx_ol = grad[i].norm(2, 1).argsort()[begin:end]

        grad_mean = grad[i][idx_ol].mean(0)
        grad_var = (grad_mean - grad[i][idx_ol]) ** 2
        norm = grad_mean.norm(2) + 1e-9
        mean_pixel_var = grad_var.sum(1)
        stats['var'].append((mean_pixel_var.sum(0) / (end - begin - 1)).item())
        match = grad_mean.sign() == grad[i][idx_ol].sign()
        stats['sign_match'].append(match.float().mean().item())

        grad_dot = (grad_mean * grad[i][idx_ol]).sum(1)
        grad_norm = grad[i][idx_ol].norm(2, 1) + 1e-9
        stats['cos'].append(torch.mean(grad_dot / (grad_norm * norm)).item())

        c = grad_var.std(0, unbiased=False)
        c1 = torch.mean(c / (grad_mean.abs() + 1e-9))
        c2 = torch.mean(c / norm)
        stats['cv1'].append(c1.item())
        stats['cv2'].append(c2.item())
    del grad
    return stats


def evaluate(net, dataloader, criterion, config, num_samples=INFTY, adv=False,
             attacks=None, return_output=False, return_adv=False, rand=True,
             num_repeats=1):
    """Evaluate `net` either on normal data or under an attack.

    Args:
        net (torch.nn.Module): Model to evaluate
        dataloader (torch.utils.data.DataLoader): DataLoader to evaluate on
        criterion (torch.nn.Module): Loss function
        config (dict): Main config
        num_samples (int, optional): Number of samples to evaluate on. Defaults
            to `float('inf')`.
        adv (bool, optional): Whether to find adversarial examples before 
            feeding data to `net`. Only used when `net` is an adversarial 
            training wrapper (e.g. PGDModel). Defaults to False.
        attack (Attack, optional): Attack object (e.g., PGDAttack, 
            RandPGDAttack). Defaults to None.
        epsilon (float, optional): Perturbation norm to used with `attack`. 
            Defaults to None.
        return_output (bool, optional): Whether to return output from `net`.
            Defaults to False.
        return_adv (bool, optional): Whether to return adversarial examples
            generated by the attack. Defaults to False.
        rand (bool, optional): Whether to apply random transforms when 
            `net` is `RandModel`. Defaults to True.
        num_repeats (int, optional): Number of times to repeat the inference.
            Defaults to 1.

    Returns:
        dict: Dictionary with keys 'loss', 'acc', 'outputs' (if `return_output` 
            is True), 'x_adv' (if `return_adv` is True), 'acc_mean', 'acc_all',
            'conf_int' (if )
    """

    net.eval()
    # Get device network is on
    device = next(net.parameters()).device
    batch_size, channel, height, width = next(iter(dataloader))[0].size()
    save_name = config["meta"]["test"].get("save_name")
    log = config['attack']['log']
    num_eval = 1        # Determine how many attacks to run in each batch
    report_steps = False
    if isinstance(attacks, list):
        # This hack allows for evaluating adv. examples at multiple steps
        if (len(attacks) == 1 and not isinstance(attacks[0], AutoAttack) and
                len(attacks[0].report_steps) > 0):
            num_eval = len(attacks[0].report_steps) + 1
            report_steps = True
        else:
            num_eval = len(attacks)

    # Compute number of batches to use based on `num_samples` (round up)
    total_batch = len(dataloader)
    if isinstance(num_samples, int):
        num_batches = int(np.ceil(abs(num_samples) / batch_size))
    else:
        num_batches = total_batch
    num_total = batch_size * num_batches

    # Initialize loop variables
    val_loss, val_total, counter = 0, 0, 1
    outputs = np.zeros((num_eval, num_repeats, num_total, config['meta']['num_classes']))
    saved_targets = np.zeros(num_total, dtype=np.int64)
    val_correct = np.zeros((num_eval, num_repeats, num_total))
    if report_steps:
        x_adv = np.zeros((1, num_total, channel, height, width))
    else:
        x_adv = np.zeros((num_eval, num_total, channel, height, width))

    # Save gradients if specified
    save_grad = config['attack'].get('save_grad', False)
    stats = {'var': [], 'cos': [], 'cv1': [], 'cv2': [], 'sign_match': []}

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(dataloader):
            begin = batch_idx * batch_size
            end = begin + inputs.size(0)

            if num_samples >= 0 and batch_idx == num_batches:
                break
            # If `num_samples` is negative, use the last `abs(num_samples)`
            # data points from `dataloader` for evaluation.
            elif num_samples < 0:
                if (batch_idx < total_batch - num_batches - 1 or
                        batch_idx == total_batch - 1):
                    continue
                begin = (batch_idx + num_batches - total_batch + 1) * batch_size
                end = begin + batch_size

            # Log during testing with attack only
            if not adv and attacks is not None:
                log.info(f'Batch {counter}/{num_batches}...')
                counter += 1

            # Make sure that there are enough samples for all devices
            num_device = torch.cuda.device_count()
            if inputs.size(0) < num_device:
                break

            if return_adv or return_output:
                saved_targets[begin:end] = targets.numpy()
            inputs, targets = inputs.to(device), targets.to(device)

            # Perturbing input with an attack if specified
            for i in range(num_eval):
                if num_eval > 1:
                    log.info(f'  Attack {i + 1}/{num_eval}')

                if save_grad:
                    grad_list = _run_attack(attacks[0], inputs, targets, return_grad=save_grad)
                    break

                if attacks is None:
                    x_ = inputs
                elif not report_steps or i == 0:
                    # Generate adversarial examples
                    x_ = _run_attack(attacks[i], inputs, targets)

                x = x_[i].detach() if report_steps else x_.detach()

                if return_adv and (not report_steps or i == 0):
                    x_adv[i, begin:end] = x.cpu().numpy()

                # If specified, repeat the inference multiple times to compute
                # confidence interval
                for j in range(num_repeats):
                    output = net(x, targets=targets, mode='test', rand=rand)
                    # Collect output for each num_repeats
                    if return_output:
                        outputs[i, j, begin:end] = output.cpu().numpy()
                    _, predicted = output.max(1)
                    val_correct[i, j, begin:end] = predicted.eq(
                        targets).float().cpu().numpy()

                # DEBUG: save intermediate results in case of unexpected crash
                if save_name is not None:
                    name = parse_model_name(config)
                    name = f'{name}_{save_name}'
                    pickle.dump(val_correct, open(f'tmp_pickle/{name}.pkl', 'wb'))

            val_total += inputs.size(0)
            # Compute stats of the gradients
            if save_grad:
                _compute_grad_stats(stats, grad_list, batch_size)
                continue

            # Loss is computed only for the last attack and the last repeat
            loss = compute_loss(net, criterion, output, targets, config,
                                rand=rand, mode='test')
            val_loss += loss.item() * inputs.size(0)
    # val_total can be smaller here if the loop breaks because of not enough
    # samples to run on gpus in parallel
    num_samples = int(min(abs(num_samples), val_total))
    val_correct = val_correct[:, :, :num_samples]

    if save_grad:
        for s in stats:
            log.info(f'{s}: mean {np.mean(stats[s]):.6f}, median {np.median(stats[s]):.6f}')
        raise NotImplementedError('Finished. Gradients stats were printed.')

    # Output dictionary with the structure: {loss, acc,
    # out_0: {acc_mean, conf_int, acc_all}, out_1: {...}, (outputs), (x_adv)}
    val_acc = val_correct.mean(2) * 100
    return_dict = {'loss': val_loss / val_total, 'acc': np.mean(val_acc)}
    for i in range(num_eval):
        out = {'acc_mean': val_acc[i].mean()}
        if num_repeats > 1:
            # Compute 90, 95, and 99 confidence interval with Student's
            # t-distribution
            out['conf_int'] = [compute_int(0.9, val_acc[i]),
                               compute_int(0.95, val_acc[i]),
                               compute_int(0.99, val_acc[i])]
            out['acc_all'] = val_acc[i]
        return_dict[f'out_{i}'] = out

    if return_output:
        return_dict['outputs'] = outputs[:, :, :num_samples]
        return_dict['targets'] = saved_targets[:num_samples]
    if return_adv:
        if report_steps:
            return_dict['x_adv'] = x_adv[-1, :num_samples]
        else:
            return_dict['x_adv'] = x_adv[:, :num_samples]
        if 'targets' not in return_dict:
            return_dict['targets'] = saved_targets[:num_samples]

    return return_dict


def main_test(config, net, dataloader, mode, log, return_output=False,
              return_adv=False, clean_only=False, adv_only=False, rand=True):
    """Main testing functionality. Calls on `evaluate`.

    Args:
        config (dict): Main config
        net (torch.nn.Module): Network to evaluate
        dataloader (torch.utils.DataLaoder): Dataloader to evaluate on
        mode (str): Evaluation mode. Only used to apply mode-specific params
            from `config['meta'][mode]`.
        log (Logger): Logger
        return_output (bool, optional): Whether to also return logits output
            from `net`. Defaults to False.
        return_adv (bool, optional): Whether to return adversarial examples
            generated by the attack. Defaults to False.
        clean_only (bool, optional): Whether to only evaluate on clean data.
            Defaults to False.
        adv_only (bool, optional): Whether to only evaluate on adv data.
            Defaults to False.
        rand (optional, bool): Whether to use random transformation. Only used 
            when `net` is `RandModel`. Defaults to True.

    Returns:
        dict: Outputs (keys: 'clean' and 'adv'). Each is a dictionary with
            'loss', 'acc', and other optional keys: 'acc_mean', 'conf_int', 
            'acc_all', 'outputs', 'x_adv'.
    """
    set_random_seed(config['meta']['seed'])
    num_samples = config['meta'][mode]['num_samples']
    num_repeats = config['meta'][mode].get('num_conf_repeats', 1) if rand else 1
    criterion = select_criterion(config, mode='test')
    output_dict = {}

    # Evaluate network on clean data
    if not adv_only:
        outputs = evaluate(net, dataloader, criterion, config,
                           num_samples=num_samples, adv=False,
                           return_output=return_output, rand=rand,
                           num_repeats=num_repeats)
        output_dict['clean'] = outputs
        log.info(
            f'[Clean] loss: {outputs["loss"]:.4f}, acc: {outputs["acc"]:.2f}')
        for key in outputs:
            if 'out_' in key:
                log.info(f'{key}: {outputs[key]}')

    if clean_only:
        return output_dict

    # Evaluate network on attack
    attacks = setup_attack(config, net, log, 'test')['attack']
    print(attacks)
    outputs = evaluate(
        net, dataloader, criterion, config, num_samples=num_samples,
        adv=False, attacks=attacks, return_output=return_output,
        return_adv=return_adv, rand=rand, num_repeats=num_repeats)
    output_dict['adv'] = outputs
    log.info(f'[Adv] loss: {outputs["loss"]:.4f}, acc: {outputs["acc"]:.2f}')
    for key in outputs:
        if 'out_' in key:
            log.info(f'{key}: {outputs[key]}')

    return output_dict


def _run_attack(attack, inputs, targets, return_grad=False):
    if isinstance(attack, PGDAttack):
        x = attack.attack_batch(inputs, targets, return_grad=return_grad)
    elif isinstance(attack, AutoAttack):
        x = attack.run_standard_evaluation(
            inputs, targets, bs=inputs.size(0))
    else:
        raise NotImplementedError('Invalid attack given.')
    return x