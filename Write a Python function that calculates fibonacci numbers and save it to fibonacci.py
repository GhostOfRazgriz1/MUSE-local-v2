def fibonacci(n):
    """
    Calculate the nth Fibonacci number.
    
    Args:
        n (int): The position in the Fibonacci sequence
        
    Returns:
        int: The nth Fibonacci number
    """
    if n <= 0:
        return 0
    elif n == 1:
        return 1
    else:
        return fibonacci(n - 1) + fibonacci(n - 2)


def fibonacci_sequence(length):
    """
    Generate a Fibonacci sequence of specified length.
    
    Args:
        length (int): Number of Fibonacci numbers to generate
        
    Returns:
        list: List of Fibonacci numbers
    """
    if length <= 0:
        return []
    elif length == 1:
        return [0]
    elif length == 2:
        return [0, 1]
    
    sequence = [0, 1]
    for i in range(2, length):
        sequence.append(sequence[i-1] + sequence[i-2])
    
    return sequence


def fibonacci_efficient(n):
    """
    Calculate the nth Fibonacci number efficiently (iterative approach).
    
    Args:
        n (int): The position in the Fibonacci sequence
        
    Returns:
        int: The nth Fibonacci number
    """
    if n <= 0:
        return 0
    elif n == 1:
        return 1
    
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    
    return b


if __name__ == "__main__":
    # Example usage
    print("First 10 Fibonacci numbers:", fibonacci_sequence(10))
    print("10th Fibonacci number:", fibonacci_efficient(10))